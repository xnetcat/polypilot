"""Data-access layer: portfolio, positions, trades, market intelligence."""
from __future__ import annotations

import json
import time

from . import config, db


def _ensure_portfolio(conn, mode: str) -> None:
    row = conn.execute("SELECT id FROM portfolio_state WHERE id = 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO portfolio_state (id, mode, starting_balance, cash) "
            "VALUES (1, ?, ?, ?)",
            (mode, config.STARTING_BALANCE_USD, config.STARTING_BALANCE_USD),
        )
        conn.commit()


def get_portfolio(mode: str | None = None) -> dict:
    mode = mode or config.MODE
    conn = db.connect(mode)
    try:
        _ensure_portfolio(conn, mode)
        _roll_day(conn)
        ps = conn.execute("SELECT * FROM portfolio_state WHERE id = 1").fetchone()
        open_rows = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(size_usd),0) exp "
            "FROM positions WHERE status='open'").fetchone()
        # The kill-switch is a function of realized PnL, not a stored flag, so
        # compute breaker_tripped live to keep this view honest. This mirrors
        # risk.check_kill_switch's comparison; risk.py stays the canonical
        # breaker but we inline the tiny check here to avoid a circular import.
        realized_pnl = ps["realized_pnl"]
        day_realized_pnl = ps["day_realized_pnl"]
        breaker_tripped = (
            -realized_pnl >= config.KILL_SWITCH_TOTAL_USD
            or -day_realized_pnl >= config.KILL_SWITCH_DAILY_USD
        )
        return {
            "mode": ps["mode"],
            "cash": round(ps["cash"], 2),
            "starting_balance": ps["starting_balance"],
            "realized_pnl": round(realized_pnl, 2),
            "trade_count": ps["trade_count"],
            "open_positions": open_rows["c"],
            "open_exposure_usd": round(open_rows["exp"], 2),
            "day_realized_pnl": round(day_realized_pnl, 2),
            "breaker_tripped": breaker_tripped,
        }
    finally:
        conn.close()


def _roll_day(conn) -> None:
    today = time.strftime("%Y-%m-%d")
    ps = conn.execute("SELECT day_date FROM portfolio_state WHERE id=1").fetchone()
    if ps and ps["day_date"] != today:
        conn.execute(
            "UPDATE portfolio_state SET day_date=?, day_realized_pnl=0 WHERE id=1",
            (today,))
        conn.commit()


def open_position(mode, *, market_id, token_id, question, category, side,
                  entry_price, size_usd, shares, fair_prob, confidence,
                  thesis, metadata=None) -> int:
    conn = db.connect(mode)
    try:
        _ensure_portfolio(conn, mode)
        cur = conn.execute(
            "INSERT INTO positions (market_id, token_id, question, category, side, "
            "entry_price, size_usd, shares, fair_prob, confidence, thesis, mode, "
            "status, opened_at, metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?, ?)",
            (market_id, token_id, question, category, side, entry_price, size_usd,
             shares, fair_prob, confidence, thesis, mode, time.time(),
             json.dumps(metadata or {})))
        pos_id = cur.lastrowid
        conn.execute(
            "INSERT INTO trades (position_id, market_id, side, action, size_usd, "
            "price, shares, mode, created_at) VALUES (?,?,?, 'buy', ?,?,?,?,?)",
            (pos_id, market_id, side, size_usd, entry_price, shares, mode, time.time()))
        conn.execute(
            "UPDATE portfolio_state SET cash = cash - ?, trade_count = trade_count + 1 "
            "WHERE id = 1", (size_usd,))
        conn.commit()
        return pos_id
    finally:
        conn.close()


def close_position(mode, position_id: int, *, exit_price: float, payout: float) -> dict:
    conn = db.connect(mode)
    try:
        _ensure_portfolio(conn, mode)
        _roll_day(conn)
        pos = conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        if pos is None or pos["status"] != "open":
            return {"error": f"position {position_id} not open"}
        pnl = payout - pos["size_usd"]
        conn.execute(
            "UPDATE positions SET status='closed', closed_at=?, exit_price=?, "
            "realized_pnl=? WHERE id=?",
            (time.time(), exit_price, pnl, position_id))
        conn.execute(
            "INSERT INTO trades (position_id, market_id, side, action, size_usd, "
            "price, shares, mode, created_at) VALUES (?,?,?, 'sell', ?,?,?,?,?)",
            (position_id, pos["market_id"], pos["side"], payout, exit_price,
             pos["shares"], mode, time.time()))
        conn.execute(
            "UPDATE portfolio_state SET cash = cash + ?, realized_pnl = realized_pnl + ?, "
            "day_realized_pnl = day_realized_pnl + ? WHERE id = 1",
            (payout, pnl, pnl))
        conn.commit()
        return {"position_id": position_id, "realized_pnl": round(pnl, 2)}
    finally:
        conn.close()


def get_positions(mode: str | None = None, status: str = "open") -> list[dict]:
    conn = db.connect(mode or config.MODE)
    try:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status=? ORDER BY opened_at DESC",
            (status,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_trade_history(mode: str | None = None, limit: int = 50) -> list[dict]:
    conn = db.connect(mode or config.MODE)
    try:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def record_evaluation(mode, *, market_id, question, category, keywords, decision,
                      side="", confidence=0.0, fair_prob=None, yes_price=0.0,
                      thesis="", stake_usd=0.0) -> int:
    """Log a market evaluation. `stake_usd` is the $ committed to the trade (0
    for skips / unsized evaluations); once the row resolves it becomes the ROI
    denominator in get_performance_stats. Optional so existing callers stay
    back-compatible (omitting it records 0.0)."""
    conn = db.connect(mode)
    try:
        cur = conn.execute(
            "INSERT INTO market_intelligence (market_id, question, category, keywords, "
            "decision, side, confidence, fair_prob, yes_price, stake_usd, thesis, "
            "evaluated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (market_id, question, category, keywords, decision, side, confidence,
             fair_prob, yes_price, stake_usd, thesis, time.time()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_evaluation_outcome(mode, market_id, outcome_side, resolution_price,
                              pnl) -> bool:
    """Backfill the outcome of the most recent unresolved evaluation for a
    market once it resolves. Returns whether a row was updated (False if there
    is no logged evaluation for this market, e.g. a position opened before
    evaluations were recorded)."""
    conn = db.connect(mode)
    try:
        row = conn.execute(
            "SELECT id FROM market_intelligence WHERE market_id=? AND resolved=0 "
            "ORDER BY evaluated_at DESC LIMIT 1", (market_id,)).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE market_intelligence SET outcome_side=?, resolution_price=?, "
            "pnl=?, resolved=1, resolved_at=? WHERE id=?",
            (outcome_side, resolution_price, pnl, time.time(), row["id"]))
        conn.commit()
        return True
    finally:
        conn.close()


def update_thesis(mode, position_id, thesis, confidence, fair_prob) -> bool:
    conn = db.connect(mode)
    try:
        cur = conn.execute(
            "UPDATE positions SET thesis=?, confidence=?, fair_prob=? "
            "WHERE id=? AND status='open'",
            (thesis, confidence, fair_prob, position_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_similar_markets(mode, text: str, limit: int = 5) -> list[dict]:
    """Keyword-overlap lookup over past evaluations. Mirrors PolyDegen's
    similar-market intelligence: rank by count of shared tokens."""
    tokens = {t for t in text.lower().split() if len(t) > 2}
    conn = db.connect(mode or config.MODE)
    try:
        rows = conn.execute("SELECT * FROM market_intelligence ORDER BY evaluated_at DESC "
                            "LIMIT 500").fetchall()
    finally:
        conn.close()
    scored = []
    for r in rows:
        kw = set((r["keywords"] or "").lower().split()) | set((r["question"] or "").lower().split())
        overlap = len(tokens & kw)
        if overlap:
            scored.append((overlap, dict(r)))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in scored[:limit]]


def _empty_stats_bucket() -> dict:
    return {
        "count": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "total_pnl": 0.0,
        "avg_pnl": 0.0,
        "total_stake": 0.0,
        "roi": 0.0,
        "pnl_turnover_ratio": 0.0,
    }


def _finalize_bucket(b: dict) -> dict:
    """Turn running sums in a bucket into the reported stats (rounded,
    JSON-serializable, no division by zero)."""
    count = b["count"]
    total_pnl = b["_total_pnl"]
    total_stake = b["_stake"]  # Σ stake_usd of staked rows; true ROI denominator
    turnover = b["_abs_pnl"]   # gross |PnL| transacted; legacy stake-free ratio
    return {
        "count": count,
        "wins": b["wins"],
        "losses": b["losses"],
        "win_rate": round(b["wins"] / count, 4) if count else 0.0,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / count, 4) if count else 0.0,
        "total_stake": round(total_stake, 2),
        # True ROI = net PnL relative to the capital actually staked. Only rows
        # with a positive stake contribute to the denominator; zero-stake rows
        # still count toward PnL/win-rate but can't define a return. Guarded so
        # no staked capital -> roi 0.0.
        "roi": round(total_pnl / total_stake, 4) if total_stake > 0 else 0.0,
        # Legacy stake-free measure: net PnL over gross |PnL| turnover. Kept for
        # transparency/back-compat now that `roi` is stake-based.
        "pnl_turnover_ratio": round(total_pnl / turnover, 4) if turnover else 0.0,
    }


def get_performance_stats(mode: str | None = None,
                          category: str | None = None) -> dict:
    """Aggregate RESOLVED evaluations in market_intelligence into win rate,
    total/avg PnL, total stake, stake-based ROI, and counts, broken down by
    category plus an overall bucket.

    "Win" means pnl > 0. ROI is the *real* return on capital: total_pnl divided
    by the summed stake_usd of resolved rows that carry a positive stake
    (guarded to 0.0 when nothing was staked). `pnl_turnover_ratio` preserves the
    older stake-free measure (net PnL over gross |PnL| turnover). An optional
    `category` filter restricts which resolved rows are considered (the overall
    bucket then reflects only that category). Returns a JSON-serializable dict::

        {"overall": {...}, "by_category": {"weather": {...}, ...}}

    Empty input yields zeroed buckets (no division by zero)."""
    conn = db.connect(mode or config.MODE)
    try:
        sql = ("SELECT category, pnl, stake_usd FROM market_intelligence "
               "WHERE resolved=1 AND pnl IS NOT NULL")
        params: tuple = ()
        if category is not None:
            sql += " AND category=?"
            params = (category,)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    def _new_running() -> dict:
        return {"count": 0, "wins": 0, "losses": 0,
                "_total_pnl": 0.0, "_abs_pnl": 0.0, "_stake": 0.0}

    overall = _new_running()
    by_cat: dict[str, dict] = {}
    for r in rows:
        pnl = r["pnl"]
        # Only positive stakes form the ROI basis; legacy rows persisted before
        # the stake column (or genuine zero-stake evaluations) contribute 0.
        stake = r["stake_usd"] or 0.0
        cat = r["category"] or ""
        bucket = by_cat.setdefault(cat, _new_running())
        for b in (overall, bucket):
            b["count"] += 1
            b["_total_pnl"] += pnl
            b["_abs_pnl"] += abs(pnl)
            if stake > 0:
                b["_stake"] += stake
            if pnl > 0:
                b["wins"] += 1
            else:
                b["losses"] += 1

    return {
        "overall": _finalize_bucket(overall) if overall["count"]
        else _empty_stats_bucket(),
        "by_category": {c: _finalize_bucket(b) for c, b in by_cat.items()},
    }
