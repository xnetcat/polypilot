# tests/test_state_enhanced.py
"""Coverage for the enhanced state layer:

  * get_portfolio computes breaker_tripped LIVE from realized/day PnL vs the
    configured kill-switch limits (the stored column is gone), and
  * get_performance_stats aggregates RESOLVED market_intelligence rows into
    win rate / ROI / PnL / counts per category (plus an overall bucket).

Uses the standard reload fixture so config -> db -> state pick up the patched
env (and db._INITIALIZED, a module-level cache, is cleared).
"""
import importlib

import pytest


@pytest.fixture
def st(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "200")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_DAILY_USD", "30")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_TOTAL_USD", "60")
    import polymarket_mcp.config as cfg
    import polymarket_mcp.db as db
    import polymarket_mcp.state as state
    for m in (cfg, db, state):
        importlib.reload(m)
    return state


def _lose(st, *, market_id, amount, mode="paper"):
    """Open a position of `amount` and close it worthless -> realized -amount."""
    pid = st.open_position(
        mode, market_id=market_id, token_id="t", question="q", category="c",
        side="YES", entry_price=0.5, size_usd=amount, shares=amount * 2,
        fair_prob=0.5, confidence=0.5, thesis="t")
    st.close_position(mode, pid, exit_price=0.0, payout=0.0)


def _win(st, *, market_id, stake, payout, mode="paper"):
    """Open a position of `stake` and close it for `payout` -> realized payout-stake."""
    pid = st.open_position(
        mode, market_id=market_id, token_id="t", question="q", category="c",
        side="YES", entry_price=0.5, size_usd=stake, shares=stake * 2,
        fair_prob=0.5, confidence=0.5, thesis="t")
    st.close_position(mode, pid, exit_price=1.0, payout=payout)


# --- breaker_tripped is now LIVE (no stored column) -------------------------

def test_breaker_false_when_no_losses(st):
    p = st.get_portfolio("paper")
    assert p["breaker_tripped"] is False


def test_breaker_trips_when_day_loss_meets_limit(st):
    # Daily limit is 30. A single $30 loss meets the daily cap (>=) and trips.
    _lose(st, market_id="m", amount=30.0)
    p = st.get_portfolio("paper")
    assert p["day_realized_pnl"] == -30.0
    assert p["breaker_tripped"] is True


def test_breaker_false_one_cent_under_daily_limit(st):
    # $29.99 day loss is under the $30 daily cap and below the $60 total cap.
    _lose(st, market_id="m", amount=29.99)
    p = st.get_portfolio("paper")
    assert p["breaker_tripped"] is False


def test_breaker_trips_when_total_loss_meets_limit(tmp_path, monkeypatch):
    # Daily kept high so only the TOTAL cap (60) can be the cause. Two $35
    # losses = -70 realized, exceeding the total cap.
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "500")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_DAILY_USD", "1000")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_TOTAL_USD", "60")
    import polymarket_mcp.config as cfg
    import polymarket_mcp.db as db
    import polymarket_mcp.state as state
    for m in (cfg, db, state):
        importlib.reload(m)
    state.get_portfolio("paper")
    assert state.get_portfolio("paper")["breaker_tripped"] is False
    _lose(state, market_id="m1", amount=35.0)
    _lose(state, market_id="m2", amount=35.0)
    p = state.get_portfolio("paper")
    assert p["realized_pnl"] == -70.0
    assert p["breaker_tripped"] is True


def test_breaker_recovers_when_wins_offset_losses(st):
    # Lose 30 (would trip daily), then win 40 -> net day/total PnL +10. The day
    # loss clamps at 0, so the LIVE breaker is False again.
    _lose(st, market_id="loss", amount=30.0)
    assert st.get_portfolio("paper")["breaker_tripped"] is True
    _win(st, market_id="win", stake=10.0, payout=50.0)
    p = st.get_portfolio("paper")
    assert p["day_realized_pnl"] == 10.0
    assert p["breaker_tripped"] is False


# --- stored breaker_tripped column is gone from the schema -------------------

def test_breaker_tripped_column_removed_from_schema(st):
    import polymarket_mcp.db as db
    conn = db.connect("paper")
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(portfolio_state)").fetchall()}
    finally:
        conn.close()
    assert "breaker_tripped" not in cols
    # Sanity: the columns we still rely on are present.
    assert {"realized_pnl", "day_realized_pnl", "cash"} <= cols


def test_breaker_tripped_not_in_schema_source(st):
    import polymarket_mcp.db as db
    assert "breaker_tripped" not in db.SCHEMA


# --- get_performance_stats ---------------------------------------------------

def _record_resolved(st, *, market_id, category, pnl, mode="paper"):
    """Record an evaluation and immediately resolve it with the given pnl."""
    st.record_evaluation(mode, market_id=market_id, question=f"q {market_id}",
                         category=category, keywords="k", decision="buy_yes",
                         side="YES", confidence=0.6, fair_prob=0.6,
                         yes_price=0.5, thesis="t")
    side = "YES" if pnl > 0 else "NO"
    st.update_evaluation_outcome(mode, market_id, side,
                                 1.0 if pnl > 0 else 0.0, pnl)


def test_performance_stats_empty_returns_zeros(st):
    stats = st.get_performance_stats("paper")
    assert stats == {
        "overall": {
            "count": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "total_pnl": 0.0, "avg_pnl": 0.0, "roi": 0.0,
        },
        "by_category": {},
    }


def test_performance_stats_unresolved_rows_are_ignored(st):
    # A recorded-but-unresolved evaluation must not count toward stats.
    st.record_evaluation("paper", market_id="m", question="q", category="weather",
                         keywords="k", decision="buy_yes", side="YES")
    stats = st.get_performance_stats("paper")
    assert stats["overall"]["count"] == 0
    assert stats["by_category"] == {}


def test_performance_stats_per_category_and_overall(st):
    # weather: +20 (win), -5 (loss)  -> count 2, wins 1, total +15, |pnl| 25
    # crypto:  +10 (win)             -> count 1, wins 1, total +10, |pnl| 10
    _record_resolved(st, market_id="w1", category="weather", pnl=20.0)
    _record_resolved(st, market_id="w2", category="weather", pnl=-5.0)
    _record_resolved(st, market_id="c1", category="crypto", pnl=10.0)

    stats = st.get_performance_stats("paper")

    weather = stats["by_category"]["weather"]
    assert weather["count"] == 2
    assert weather["wins"] == 1
    assert weather["losses"] == 1
    assert weather["win_rate"] == 0.5
    assert weather["total_pnl"] == 15.0
    assert weather["avg_pnl"] == 7.5
    assert weather["roi"] == round(15.0 / 25.0, 4)  # 0.6

    crypto = stats["by_category"]["crypto"]
    assert crypto["count"] == 1
    assert crypto["wins"] == 1
    assert crypto["losses"] == 0
    assert crypto["win_rate"] == 1.0
    assert crypto["total_pnl"] == 10.0
    assert crypto["roi"] == 1.0  # all profit, no losing turnover

    overall = stats["overall"]
    assert overall["count"] == 3
    assert overall["wins"] == 2
    assert overall["losses"] == 1
    assert overall["win_rate"] == round(2 / 3, 4)
    assert overall["total_pnl"] == 25.0
    assert overall["avg_pnl"] == round(25.0 / 3, 4)
    assert overall["roi"] == round(25.0 / 35.0, 4)  # net 25 over |pnl| turnover 35


def test_performance_stats_zero_pnl_counts_as_loss(st):
    # "Win" is strictly pnl > 0, so a break-even (0.0) resolved row is a loss.
    _record_resolved(st, market_id="z1", category="weather", pnl=0.0)
    weather = st.get_performance_stats("paper")["by_category"]["weather"]
    assert weather["count"] == 1
    assert weather["wins"] == 0
    assert weather["losses"] == 1
    assert weather["win_rate"] == 0.0
    assert weather["total_pnl"] == 0.0
    assert weather["roi"] == 0.0  # zero turnover -> guarded ROI


def test_performance_stats_category_filter(st):
    _record_resolved(st, market_id="w1", category="weather", pnl=20.0)
    _record_resolved(st, market_id="c1", category="crypto", pnl=10.0)

    stats = st.get_performance_stats("paper", category="weather")
    # Only weather is considered; overall reflects just the filtered rows.
    assert set(stats["by_category"]) == {"weather"}
    assert stats["overall"]["count"] == 1
    assert stats["overall"]["total_pnl"] == 20.0


def test_performance_stats_is_json_serializable(st):
    import json
    _record_resolved(st, market_id="w1", category="weather", pnl=20.0)
    json.dumps(st.get_performance_stats("paper"))  # must not raise
