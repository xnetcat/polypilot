# tests/test_stake_roi.py
"""Stake-based (real) ROI in get_performance_stats.

ROI is now total_pnl / total_stake (the capital actually committed), per
category and overall, instead of the old PnL-turnover ratio. These tests pin
that contract: record evaluations with stakes, resolve them via
update_evaluation_outcome, and assert ROI == Sum(pnl) / Sum(stake), win rates,
and the guarded zero/empty cases.

Uses the standard config -> db -> state reload fixture so each test gets a
fresh temp DB (and db._INITIALIZED, a module-level cache, is cleared)."""
import importlib

import pytest


@pytest.fixture
def st(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "1000")
    import polymarket_mcp.config as cfg
    import polymarket_mcp.db as db
    import polymarket_mcp.state as state
    for m in (cfg, db, state):
        importlib.reload(m)
    return state


def _resolve(st, *, market_id, category, pnl, stake, mode="paper"):
    """Record an evaluation with `stake` committed, then resolve it with `pnl`."""
    st.record_evaluation(mode, market_id=market_id, question=f"q {market_id}",
                         category=category, keywords="k", decision="buy_yes",
                         side="YES", confidence=0.6, fair_prob=0.6,
                         yes_price=0.5, thesis="t", stake_usd=stake)
    side = "YES" if pnl > 0 else "NO"
    st.update_evaluation_outcome(mode, market_id, side,
                                 1.0 if pnl > 0 else 0.0, pnl)


# --- The headline case from the spec ----------------------------------------

def test_single_resolved_buy_yes_roi_is_pnl_over_stake(st):
    # A resolved buy_yes with stake_usd=20 and pnl=+10 -> roi 0.5.
    _resolve(st, market_id="m1", category="weather", pnl=10.0, stake=20.0)
    bucket = st.get_performance_stats("paper")["by_category"]["weather"]
    assert bucket["total_pnl"] == 10.0
    assert bucket["total_stake"] == 20.0
    assert bucket["roi"] == 0.5
    assert bucket["win_rate"] == 1.0


# --- Per-category and overall ROI = Sum(pnl) / Sum(stake) --------------------

def test_per_category_and_overall_roi_is_sum_pnl_over_sum_stake(st):
    # weather: +30 on $100, -10 on $40  -> pnl +20, stake $140
    # crypto:  +25 on  $50              -> pnl +25, stake  $50
    _resolve(st, market_id="w1", category="weather", pnl=30.0, stake=100.0)
    _resolve(st, market_id="w2", category="weather", pnl=-10.0, stake=40.0)
    _resolve(st, market_id="c1", category="crypto", pnl=25.0, stake=50.0)

    stats = st.get_performance_stats("paper")

    weather = stats["by_category"]["weather"]
    assert weather["count"] == 2
    assert weather["wins"] == 1
    assert weather["losses"] == 1
    assert weather["win_rate"] == 0.5
    assert weather["total_pnl"] == 20.0
    assert weather["total_stake"] == 140.0
    assert weather["roi"] == round(20.0 / 140.0, 4)

    crypto = stats["by_category"]["crypto"]
    assert crypto["count"] == 1
    assert crypto["win_rate"] == 1.0
    assert crypto["total_pnl"] == 25.0
    assert crypto["total_stake"] == 50.0
    assert crypto["roi"] == 0.5

    overall = stats["overall"]
    assert overall["count"] == 3
    assert overall["wins"] == 2
    assert overall["losses"] == 1
    assert overall["win_rate"] == round(2 / 3, 4)
    assert overall["total_pnl"] == 45.0
    assert overall["total_stake"] == 190.0
    assert overall["roi"] == round(45.0 / 190.0, 4)


def test_losing_book_has_negative_roi(st):
    # Net loser: -40 on $80 staked -> roi -0.5.
    _resolve(st, market_id="l1", category="sports", pnl=-40.0, stake=80.0)
    bucket = st.get_performance_stats("paper")["by_category"]["sports"]
    assert bucket["total_pnl"] == -40.0
    assert bucket["total_stake"] == 80.0
    assert bucket["roi"] == -0.5
    assert bucket["win_rate"] == 0.0


# --- Guarded zero / empty cases ---------------------------------------------

def test_empty_returns_zeroed_roi_without_error(st):
    stats = st.get_performance_stats("paper")
    assert stats["overall"]["roi"] == 0.0
    assert stats["overall"]["total_stake"] == 0.0
    assert stats["overall"]["count"] == 0
    assert stats["by_category"] == {}


def test_zero_stake_rows_give_zero_roi_but_still_count(st):
    # Resolved rows with no stake (e.g. legacy / unsized) still count toward
    # PnL, wins and win_rate, but the ROI denominator is 0 -> guarded 0.0.
    _resolve(st, market_id="z1", category="weather", pnl=15.0, stake=0.0)
    _resolve(st, market_id="z2", category="weather", pnl=-5.0, stake=0.0)
    bucket = st.get_performance_stats("paper")["by_category"]["weather"]
    assert bucket["count"] == 2
    assert bucket["wins"] == 1
    assert bucket["losses"] == 1
    assert bucket["win_rate"] == 0.5
    assert bucket["total_pnl"] == 10.0
    assert bucket["total_stake"] == 0.0
    assert bucket["roi"] == 0.0  # no staked capital -> guarded


def test_only_staked_rows_form_roi_denominator(st):
    # A staked winner plus a zero-stake winner in the same category: the
    # zero-stake row inflates total_pnl but not total_stake, so ROI is computed
    # purely off the staked capital.
    _resolve(st, market_id="s1", category="news", pnl=10.0, stake=20.0)
    _resolve(st, market_id="s2", category="news", pnl=10.0, stake=0.0)
    bucket = st.get_performance_stats("paper")["by_category"]["news"]
    assert bucket["count"] == 2
    assert bucket["total_pnl"] == 20.0
    assert bucket["total_stake"] == 20.0
    # ROI = total_pnl(20) / total_stake(20) = 1.0
    assert bucket["roi"] == 1.0


# --- Back-compat: stake column persists and defaults to 0 -------------------

def test_record_evaluation_without_stake_defaults_to_zero(st):
    import polymarket_mcp.db as db
    # Existing-style call that omits stake_usd entirely.
    st.record_evaluation("paper", market_id="m1", question="q", category="c",
                         keywords="k", decision="skip")
    conn = db.connect("paper")
    try:
        row = conn.execute(
            "SELECT stake_usd FROM market_intelligence WHERE market_id='m1'"
        ).fetchone()
    finally:
        conn.close()
    assert row["stake_usd"] == 0.0


def test_record_evaluation_persists_stake(st):
    import polymarket_mcp.db as db
    st.record_evaluation("paper", market_id="m2", question="q", category="c",
                         keywords="k", decision="buy_yes", side="YES",
                         stake_usd=42.5)
    conn = db.connect("paper")
    try:
        row = conn.execute(
            "SELECT stake_usd FROM market_intelligence WHERE market_id='m2'"
        ).fetchone()
    finally:
        conn.close()
    assert row["stake_usd"] == 42.5


def test_performance_stats_with_stake_is_json_serializable(st):
    import json
    _resolve(st, market_id="w1", category="weather", pnl=20.0, stake=40.0)
    json.dumps(st.get_performance_stats("paper"))  # must not raise
