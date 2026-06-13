"""Coverage for polymarket_mcp.replay.calibrate_probability.

``calibrate_probability`` maps a raw model probability to the agent's own
historical accuracy: it finds the decile bucket the raw prob lands in, then
returns the actual win rate (``pnl > 0``) of past RESOLVED trades whose
predicted prob for the CHOSEN side (``fair_prob`` for buy_yes, ``1 - fair_prob``
for buy_no) fell in that same bucket. A min-sample guard (n < 5) falls back to
the raw prob unchanged.

Reuses the same reload fixture + seeding helpers as test_replay (temp DB per
test, no network).
"""
import json

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Reloaded (config, db, state, replay) bound to a temp DB dir."""
    import importlib

    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "200")
    import polymarket_mcp.config as cfg
    import polymarket_mcp.db as db
    import polymarket_mcp.state as state
    import polymarket_mcp.replay as replay
    for m in (cfg, db, state, replay):
        importlib.reload(m)
    return type("Env", (), {"cfg": cfg, "db": db, "state": state, "replay": replay})


def _record_resolved(env, *, market_id, decision, fair_prob, pnl,
                     category="c", stake=10.0, mode="paper"):
    """Record a trade evaluation then resolve it with the given pnl.

    `decision` is buy_yes/buy_no; `fair_prob` is the model's P(YES). Outcome is
    a win iff pnl > 0 (the only thing calibration keys off, besides the
    chosen-side predicted prob)."""
    env.state.record_evaluation(
        mode, market_id=market_id, question=f"q {market_id}", category=category,
        keywords="k", decision=decision, side=("YES" if decision == "buy_yes" else "NO"),
        confidence=0.6, fair_prob=fair_prob, yes_price=0.5, thesis="t",
        stake_usd=stake)
    won = pnl > 0
    env.state.update_evaluation_outcome(
        mode, market_id, "YES" if won else "NO", 1.0 if won else 0.0, pnl)


def _seed_bucket(env, *, fair_prob, n, wins, decision="buy_yes", prefix="m"):
    """Seed `n` resolved buy_yes trades all at `fair_prob` (so the chosen-side
    predicted prob shares one decile), `wins` of which won."""
    for i in range(n):
        _record_resolved(
            env, market_id=f"{prefix}{i}", decision=decision, fair_prob=fair_prob,
            pnl=(1.0 if i < wins else -1.0))


# --------------------------------------------------------------------------
# min-sample guard / empty DB
# --------------------------------------------------------------------------

def test_empty_db_returns_raw_with_insufficient_history_note(env):
    out = env.replay.calibrate_probability(0.62, mode="paper")
    assert out["raw"] == 0.62
    assert out["calibrated"] == 0.62
    assert out["samples"] == 0
    assert out["note"] == "insufficient history (n<5) — using raw"


def test_fewer_than_5_samples_falls_back_to_raw(env):
    # Only 4 resolved trades in the 0.7-0.8 bucket -> below the n>=5 threshold.
    _seed_bucket(env, fair_prob=0.75, n=4, wins=4)
    out = env.replay.calibrate_probability(0.72, mode="paper")
    assert out["samples"] == 4
    assert out["calibrated"] == out["raw"] == 0.72
    assert out["note"] == "insufficient history (n<5) — using raw"


def test_other_buckets_do_not_count_toward_target_bucket(env):
    # 10 trades, but in the 0.2-0.3 bucket; the 0.7-0.8 query bucket is empty.
    _seed_bucket(env, fair_prob=0.25, n=10, wins=5)
    out = env.replay.calibrate_probability(0.75, mode="paper")
    assert out["samples"] == 0
    assert out["calibrated"] == out["raw"] == 0.75


# --------------------------------------------------------------------------
# calibration with sufficient history
# --------------------------------------------------------------------------

def test_calibrated_equals_bucket_win_rate_when_n_ge_5(env):
    # 10 resolved buy_yes trades at fair 0.75 (bucket 0.7-0.8); 7 won.
    # Bucket win rate = 0.7. A raw of 0.72 (same bucket) calibrates to 0.7.
    _seed_bucket(env, fair_prob=0.75, n=10, wins=7)
    out = env.replay.calibrate_probability(0.72, mode="paper")
    assert out["samples"] == 10
    assert out["calibrated"] == 0.7
    assert out["raw"] == 0.72
    assert "70" in out["note"]  # win-rate percentage surfaced in the note


def test_exactly_5_samples_calibrates(env):
    # Threshold boundary: exactly 5 trades is enough. 5 trades, 2 wins -> 0.4.
    _seed_bucket(env, fair_prob=0.55, n=5, wins=2)
    out = env.replay.calibrate_probability(0.58, mode="paper")
    assert out["samples"] == 5
    assert out["calibrated"] == 0.4


def test_buy_no_predicted_prob_uses_complement(env):
    # buy_no trades at fair_prob 0.2 -> chosen-side predicted prob = 1-0.2 = 0.8,
    # which lands in the 0.8-0.9 bucket. A raw query of 0.85 must match them.
    _seed_bucket(env, fair_prob=0.2, n=8, wins=6, decision="buy_no")
    out = env.replay.calibrate_probability(0.85, mode="paper")
    assert out["samples"] == 8
    assert out["calibrated"] == round(6 / 8, 4)  # 0.75
    # A buy_yes-style query at the raw fair_prob 0.2 itself sees no history.
    other = env.replay.calibrate_probability(0.2, mode="paper")
    assert other["samples"] == 0
    assert other["calibrated"] == other["raw"] == 0.2


def test_mixed_decisions_share_bucket_by_chosen_side_prob(env):
    # buy_yes at 0.85 (pred 0.85) and buy_no at 0.15 (pred 0.85) BOTH land in
    # the 0.8-0.9 bucket and are pooled together.
    _record_resolved(env, market_id="y0", decision="buy_yes", fair_prob=0.85, pnl=1.0)
    _record_resolved(env, market_id="y1", decision="buy_yes", fair_prob=0.85, pnl=1.0)
    _record_resolved(env, market_id="y2", decision="buy_yes", fair_prob=0.85, pnl=-1.0)
    _record_resolved(env, market_id="n0", decision="buy_no", fair_prob=0.15, pnl=1.0)
    _record_resolved(env, market_id="n1", decision="buy_no", fair_prob=0.15, pnl=-1.0)
    # 5 pooled trades in 0.8-0.9, 3 wins -> 0.6.
    out = env.replay.calibrate_probability(0.88, mode="paper")
    assert out["samples"] == 5
    assert out["calibrated"] == 0.6


# --------------------------------------------------------------------------
# robustness
# --------------------------------------------------------------------------

def test_ignores_skips_and_unresolved(env):
    # A resolved skip and an unresolved buy_yes in the target bucket must not
    # count; only the 5 real resolved trades do.
    _seed_bucket(env, fair_prob=0.75, n=5, wins=5)
    # Resolved skip in the same nominal prob bucket:
    env.state.record_evaluation("paper", market_id="skip1", question="q",
        category="c", keywords="k", decision="skip", side="", confidence=0.5,
        fair_prob=0.75, yes_price=0.5, thesis="t", stake_usd=0.0)
    env.state.update_evaluation_outcome("paper", "skip1", "YES", 1.0, 0.0)
    # Unresolved buy_yes:
    env.state.record_evaluation("paper", market_id="open1", question="q",
        category="c", keywords="k", decision="buy_yes", side="YES", confidence=0.6,
        fair_prob=0.75, yes_price=0.5, thesis="t", stake_usd=10.0)
    out = env.replay.calibrate_probability(0.75, mode="paper")
    assert out["samples"] == 5
    assert out["calibrated"] == 1.0


def test_top_edge_prob_lands_in_last_bucket(env):
    # raw=1.0 must map into the 0.9-1.0 bucket (no overflow), matching trades
    # whose chosen-side prob is also ~1.0.
    _seed_bucket(env, fair_prob=0.95, n=6, wins=3)
    out = env.replay.calibrate_probability(1.0, mode="paper")
    assert out["raw"] == 1.0
    assert out["samples"] == 6
    assert out["calibrated"] == 0.5


def test_out_of_range_and_non_finite_raw_is_clamped(env):
    # Out-of-range / NaN inputs must not raise; raw is clamped into [0, 1].
    assert env.replay.calibrate_probability(1.7, mode="paper")["raw"] == 1.0
    assert env.replay.calibrate_probability(-0.4, mode="paper")["raw"] == 0.0
    assert env.replay.calibrate_probability(float("nan"), mode="paper")["raw"] == 0.0


def test_result_is_json_serializable(env):
    _seed_bucket(env, fair_prob=0.55, n=6, wins=3)
    out = env.replay.calibrate_probability(0.52, mode="paper")
    assert json.loads(json.dumps(out)) == out
