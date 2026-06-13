"""Backtesting / replay scorecard.

Decision-quality analytics over the agent's own RESOLVED trades plus a helper
to pull real recently-resolved Polymarket markets to backtest against.

Two public entry points:

* :func:`scorecard` — reads RESOLVED ``market_intelligence`` rows that were
  actual trades (``decision`` in ``{"buy_yes", "buy_no"}``) straight from the
  SQLite DB and reports win rate, ROI, a Brier score, log loss, and a
  calibration table (so the agent can see whether it is over/under-confident),
  broken down overall and per category.
* :func:`fetch_resolved_markets` — best-effort fetch of recently CLOSED markets
  from the Gamma API, surfacing the winning outcome when it can be inferred from
  the resolved token prices (a token priced ~1.0 won, ~0.0 lost).

This module reads the DB directly via :func:`db.connect`; it never mutates
``state.py`` or the schema.
"""
from __future__ import annotations

import json
import logging
import math

import httpx

from . import config, db

log = logging.getLogger(__name__)

TRADE_DECISIONS = ("buy_yes", "buy_no")

# Clamp for log loss so ln(p) / ln(1-p) never hit a math-domain error at p in
# {0, 1}. Brier needs no clamp (it is a plain squared error).
_P_FLOOR = 1e-6
_P_CEIL = 1.0 - 1e-6

# Number of calibration buckets: deciles over [0, 1].
_N_BUCKETS = 10

# Minimum TOTAL resolved trades before calibration kicks in at all. Below this
# there is too little history to trust, so we fall back to the raw prob.
_MIN_CALIBRATION_SAMPLES = 5

# Beta-shrinkage prior strength (pseudo-count): a sparse bucket's empirical win
# rate is pulled toward the global base rate with this much weight, so thin
# buckets don't over-react. ~5 means a bucket needs ~5 in-bucket trades to move
# roughly halfway from the base rate to its own empirical rate.
_CALIBRATION_PRIOR_STRENGTH = 5.0

# Fractional weight given to each immediate neighbor bucket when smoothing a thin
# target bucket (borrowing strength from adjacent deciles).
_CALIBRATION_NEIGHBOR_WEIGHT = 0.5


def _chosen_side_prob(decision: str, fair_prob: float) -> float:
    """Predicted probability that the CHOSEN side wins.

    For a ``buy_yes`` trade that is the model's ``fair_prob`` (its estimate that
    YES resolves true); for ``buy_no`` it is the complementary ``1 - fair_prob``
    (its estimate that NO resolves true). Callers must pass a non-null
    ``fair_prob``.
    """
    return fair_prob if decision == "buy_yes" else 1.0 - fair_prob


def _empty_scorecard_bucket() -> dict:
    """A fully-zeroed bucket — used for empty input and empty categories so the
    shape is stable and there is never a division by zero."""
    return {
        "count": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "total_pnl": 0.0,
        "total_stake": 0.0,
        "roi": 0.0,
        "brier": 0.0,
        "log_loss": 0.0,
        "calibration": _empty_calibration(),
    }


def _empty_calibration() -> list[dict]:
    """Ten empty decile buckets (0.0-0.1, ..., 0.9-1.0). Always returned so the
    table shape is stable even with no predictions to bucket."""
    table = []
    for i in range(_N_BUCKETS):
        lo = i / _N_BUCKETS
        hi = (i + 1) / _N_BUCKETS
        table.append({
            "bucket": f"{lo:.1f}-{hi:.1f}",
            "lo": round(lo, 4),
            "hi": round(hi, 4),
            "count": 0,
            "mean_predicted": 0.0,
            "actual_win_rate": 0.0,
        })
    return table


def _bucket_index(p: float) -> int:
    """Decile index for a predicted probability. ``p`` is assumed in [0, 1];
    the right edge (1.0) lands in the top bucket rather than overflowing."""
    idx = int(p * _N_BUCKETS)
    if idx >= _N_BUCKETS:
        idx = _N_BUCKETS - 1
    if idx < 0:
        idx = 0
    return idx


def _new_running() -> dict:
    """Running accumulators for one bucket. ``_preds``/``_actuals`` are kept so
    Brier, log loss and calibration can be computed after the full scan."""
    return {
        "count": 0,
        "wins": 0,
        "losses": 0,
        "_total_pnl": 0.0,
        "_stake": 0.0,
        "_preds": [],
        "_actuals": [],
    }


def _finalize(b: dict) -> dict:
    """Turn a running bucket into reported, JSON-serializable stats.

    Brier and log loss are computed only over rows that carried a non-null
    ``fair_prob`` (collected in ``_preds``/``_actuals``); a bucket with trades
    but no usable probabilities reports 0.0 for both rather than dividing by
    zero. ROI divides net PnL by summed positive stake, guarded to 0.0."""
    count = b["count"]
    total_pnl = b["_total_pnl"]
    total_stake = b["_stake"]
    preds = b["_preds"]
    actuals = b["_actuals"]
    n_prob = len(preds)

    brier = 0.0
    log_loss = 0.0
    if n_prob:
        brier = sum((p - a) ** 2 for p, a in zip(preds, actuals)) / n_prob
        ll_sum = 0.0
        for p, a in zip(preds, actuals):
            pc = min(max(p, _P_FLOOR), _P_CEIL)
            ll_sum += -(a * math.log(pc) + (1 - a) * math.log(1 - pc))
        log_loss = ll_sum / n_prob

    return {
        "count": count,
        "wins": b["wins"],
        "losses": b["losses"],
        "win_rate": round(b["wins"] / count, 4) if count else 0.0,
        "total_pnl": round(total_pnl, 2),
        "total_stake": round(total_stake, 2),
        "roi": round(total_pnl / total_stake, 4) if total_stake > 0 else 0.0,
        "brier": round(brier, 6),
        "log_loss": round(log_loss, 6),
        "calibration": _calibration_table(preds, actuals),
    }


def _calibration_table(preds: list[float], actuals: list[float]) -> list[dict]:
    """Per-decile calibration: count, mean predicted prob, and actual win rate.

    The summed ``count`` across buckets equals ``len(preds)`` (every prediction
    lands in exactly one decile), which lets a caller cross-check that the
    table covers all probability-bearing rows."""
    table = _empty_calibration()
    sums = [0.0] * _N_BUCKETS
    wins = [0.0] * _N_BUCKETS
    counts = [0] * _N_BUCKETS
    for p, a in zip(preds, actuals):
        idx = _bucket_index(p)
        counts[idx] += 1
        sums[idx] += p
        wins[idx] += a
    for i in range(_N_BUCKETS):
        if counts[i]:
            table[i]["count"] = counts[i]
            table[i]["mean_predicted"] = round(sums[i] / counts[i], 4)
            table[i]["actual_win_rate"] = round(wins[i] / counts[i], 4)
    return table


def scorecard(mode: str | None = None) -> dict:
    """Decision-quality scorecard over RESOLVED trades.

    Considers only ``market_intelligence`` rows that are resolved
    (``resolved=1`` with a non-null ``pnl``) AND were actual trades
    (``decision`` in ``{"buy_yes", "buy_no"}`` — skips/holds are excluded).
    For each row the predicted probability is that the CHOSEN side wins
    (``fair_prob`` for buy_yes, ``1 - fair_prob`` for buy_no) and the realized
    outcome is 1 if ``pnl > 0`` else 0.

    Reports, overall and per ``category``:

    * ``count``, ``wins`` (pnl>0), ``losses``, ``win_rate``
    * ``total_pnl``, ``total_stake``, ``roi`` (Σpnl / Σstake, guarded)
    * ``brier`` — mean squared error of predicted vs actual (lower is better)
    * ``log_loss`` — mean negative log-likelihood with p clamped to
      ``[1e-6, 1-1e-6]``
    * ``calibration`` — 10 decile buckets, each with count / mean predicted /
      actual win rate

    Brier/log-loss/calibration only use rows with a non-null ``fair_prob``
    (rows missing it still count toward win-rate/PnL/ROI). Empty input — or a DB
    with no resolved trades — yields fully-zeroed buckets and an empty
    ``by_category`` map; there are no divide-by-zero or math-domain errors.

    Returns a JSON-serializable dict::

        {"overall": {...}, "by_category": {"weather": {...}, ...}}
    """
    placeholders = ",".join("?" for _ in TRADE_DECISIONS)
    conn = db.connect(mode or config.MODE)
    try:
        rows = conn.execute(
            "SELECT category, decision, fair_prob, pnl, stake_usd "
            "FROM market_intelligence "
            "WHERE resolved=1 AND pnl IS NOT NULL "
            f"AND decision IN ({placeholders})",
            TRADE_DECISIONS,
        ).fetchall()
    finally:
        conn.close()

    overall = _new_running()
    by_cat: dict[str, dict] = {}

    for r in rows:
        pnl = r["pnl"]
        stake = r["stake_usd"] or 0.0
        cat = r["category"] or ""
        actual = 1.0 if pnl > 0 else 0.0
        bucket = by_cat.setdefault(cat, _new_running())

        for b in (overall, bucket):
            b["count"] += 1
            b["_total_pnl"] += pnl
            if stake > 0:
                b["_stake"] += stake
            if pnl > 0:
                b["wins"] += 1
            else:
                b["losses"] += 1
            # Probabilistic metrics need a fair_prob; rows without one still
            # count toward win-rate / PnL / ROI above but are skipped here.
            if r["fair_prob"] is not None:
                pred = _chosen_side_prob(r["decision"], r["fair_prob"])
                b["_preds"].append(pred)
                b["_actuals"].append(actual)

    return {
        "overall": _finalize(overall) if overall["count"]
        else _empty_scorecard_bucket(),
        "by_category": {c: _finalize(b) for c, b in by_cat.items()},
    }


def _clamp01(x: float) -> float:
    """Clamp a probability into [0, 1]."""
    return max(0.0, min(1.0, x))


def calibrate_probability(fair_prob: float, mode: str | None = None) -> dict:
    """Calibrate a model probability against the agent's own resolved track record.

    Looks at every RESOLVED ``market_intelligence`` row that was an actual trade
    (``decision`` in ``{"buy_yes", "buy_no"}``, ``resolved=1``, ``pnl`` not null)
    and, for each, the predicted probability that its CHOSEN side won
    (``fair_prob`` for ``buy_yes``, ``1 - fair_prob`` for ``buy_no``). It buckets
    those predictions into the same deciles as :func:`scorecard`'s calibration
    table and estimates the historical win rate of the bucket that ``fair_prob``
    falls in — but, unlike a raw bucket win rate, it makes that estimate robust at
    low sample sizes two ways:

    * **Neighbor smoothing** — the two adjacent decile buckets contribute at
      ``_CALIBRATION_NEIGHBOR_WEIGHT`` (0.5) each, so a thin target bucket borrows
      strength from its neighbors instead of swinging on one or two trades.
    * **Beta shrinkage** — the smoothed win rate is pulled toward the GLOBAL base
      rate (overall win rate across all resolved trades) with a Beta prior of
      strength ``_CALIBRATION_PRIOR_STRENGTH`` (5 pseudo-counts):
      ``calibrated = (wins + k·base) / (total + k)``. Sparse buckets stay near the
      base rate; well-populated buckets approach their own empirical rate.

    Global guard: with fewer than ``_MIN_CALIBRATION_SAMPLES`` (5) resolved trades
    in total — including an empty DB — there is too little history to trust, so
    ``calibrated`` equals ``raw``. Never raises and never divides by zero; a
    non-finite or out-of-range ``fair_prob`` is clamped to [0, 1] first.

    Returns a JSON-serializable dict::

        {"raw": <input>, "calibrated": <float>, "samples": <in-bucket count>,
         "base_rate": <float|None>, "note": <str>}
    """
    try:
        raw = float(fair_prob)
    except (TypeError, ValueError):
        raw = 0.0
    # Reject NaN/inf BEFORE clamping (min/max on NaN is undefined), then clamp.
    raw = _clamp01(raw) if math.isfinite(raw) else 0.0

    target_bucket = _bucket_index(raw)

    placeholders = ",".join("?" for _ in TRADE_DECISIONS)
    conn = db.connect(mode or config.MODE)
    try:
        rows = conn.execute(
            "SELECT decision, fair_prob, pnl "
            "FROM market_intelligence "
            "WHERE resolved=1 AND pnl IS NOT NULL AND fair_prob IS NOT NULL "
            f"AND decision IN ({placeholders})",
            TRADE_DECISIONS,
        ).fetchall()
    finally:
        conn.close()

    # Per-bucket win/total of past predictions' chosen-side prob (and the global
    # base rate). ``_bucket_index`` clamps, so no overflow on edge probs.
    wins = [0] * _N_BUCKETS
    totals = [0] * _N_BUCKETS
    global_wins = 0
    for r in rows:
        pred = _chosen_side_prob(r["decision"], r["fair_prob"])
        b = _bucket_index(pred)
        totals[b] += 1
        if r["pnl"] > 0:
            wins[b] += 1
            global_wins += 1

    total = len(rows)
    target_total = totals[target_bucket]

    if total < _MIN_CALIBRATION_SAMPLES:
        return {
            "raw": raw,
            "calibrated": raw,
            "samples": target_total,
            "base_rate": None,
            "note": "insufficient history (n<5) — using raw",
        }

    base = global_wins / total  # total >= 5, no div-by-zero

    # Neighbor-smoothed counts: target bucket at full weight, immediate neighbors
    # at the neighbor weight (borrow strength from adjacent deciles).
    eff_wins = float(wins[target_bucket])
    eff_total = float(target_total)
    for nb in (target_bucket - 1, target_bucket + 1):
        if 0 <= nb < _N_BUCKETS:
            eff_wins += _CALIBRATION_NEIGHBOR_WEIGHT * wins[nb]
            eff_total += _CALIBRATION_NEIGHBOR_WEIGHT * totals[nb]

    # No evidence in the target bucket OR its neighbors: we have history, but none
    # about this probability region, so don't recalibrate — keep the raw value
    # rather than yanking it to the global base rate.
    if eff_total == 0:
        return {
            "raw": raw,
            "calibrated": raw,
            "samples": target_total,
            "base_rate": round(base, 6),
            "note": "no nearby resolved history — using raw",
        }

    # Beta shrinkage toward the base rate.
    k = _CALIBRATION_PRIOR_STRENGTH
    calibrated = _clamp01((eff_wins + k * base) / (eff_total + k))
    return {
        "raw": raw,
        "calibrated": round(calibrated, 6),
        "samples": target_total,
        "base_rate": round(base, 6),
        "note": (
            f"beta-shrunk toward base rate {base:.1%} (prior k={k:g}, "
            f"neighbor-smoothed); {target_total} in-bucket of {total} resolved"
        ),
    }


def _parse_prices(raw) -> list[float]:
    """Parse a Gamma ``outcomePrices`` value (a JSON-encoded string list, a real
    list, or already-numeric entries) into floats. Returns ``[]`` on any
    failure so a single bad row never aborts the whole fetch."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    out: list[float] = []
    for v in raw:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            out.append(float("nan"))
    return out


def _parse_outcomes(raw) -> list[str]:
    """Parse a Gamma ``outcomes`` value (JSON string list or real list) into
    label strings. Returns ``[]`` on failure."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    return [str(v) for v in raw]


def _winning_outcome(outcomes: list[str], prices: list[float]) -> str | None:
    """Infer the resolved/winning outcome from resolved token prices.

    A resolved binary/multi market settles its winning token to ~1.0 and the
    losers to ~0.0. Return the label of the token priced ~1.0; ``None`` if no
    token is clearly the winner (e.g. not yet settled, or ambiguous prices)."""
    best_label: str | None = None
    for i, price in enumerate(prices):
        if price != price:  # NaN guard
            continue
        if price >= 0.99 and i < len(outcomes):
            if best_label is not None:
                # Two tokens both ~1.0 -> ambiguous, can't call a winner.
                return None
            best_label = outcomes[i]
    return best_label


def fetch_resolved_markets(limit: int = 20) -> list[dict]:
    """Fetch recently CLOSED/resolved markets from the Gamma API.

    Hits ``{GAMMA_API}/markets?closed=true&limit=...&order=volume&ascending=false``
    and returns, per market, a dict with ``market_id``, ``question``,
    ``category`` and ``winning_outcome`` (the label of the token that resolved
    ~1.0, or ``None`` if it can't be determined). These are real resolved
    markets the agent can backtest/practice against.

    Best-effort: a network/parse error for the whole request returns ``[]``;
    per-row parse failures are skipped without aborting the rest.
    """
    try:
        resp = httpx.get(
            f"{config.GAMMA_API}/markets",
            params={
                "closed": True,
                "limit": limit,
                "order": "volume",
                "ascending": False,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001 - best-effort, never raise to caller
        log.warning("[replay] Gamma resolved-markets fetch failed: %s", e)
        return []

    items = data if isinstance(data, list) else data.get("data", [])
    results: list[dict] = []
    for m in items:
        try:
            question = (m.get("question") or "").strip()
            market_id = m.get("conditionId", m.get("condition_id", m.get("id", "")))
            if not question or not market_id:
                continue
            outcomes = _parse_outcomes(m.get("outcomes", []))
            prices = _parse_prices(m.get("outcomePrices", []))
            from .markets import _infer_category  # local import: avoid cycle
            results.append({
                "market_id": str(market_id),
                "question": question,
                "category": _infer_category(question, m.get("tags", None) or []),
                "winning_outcome": _winning_outcome(outcomes, prices),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return results
