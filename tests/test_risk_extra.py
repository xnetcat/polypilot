# tests/test_risk_extra.py
"""Breadth coverage for the cumulative-loss kill-switch in risk.py.

Uses a real on-disk SQLite DB via the reload fixture: set the POLYPILOT_*
env vars, then reload config -> db -> state -> risk so that all module-level
config constants and DB paths pick up the test environment. Realized PnL is
driven by opening and closing positions through state.open_position /
state.close_position.

All tests assert the CURRENT correct behaviour and must pass.
"""
import importlib

import pytest


def _reload_stack():
    import polymarket_mcp.config as cfg
    import polymarket_mcp.db as db
    import polymarket_mcp.state as state
    import polymarket_mcp.risk as risk
    for m in (cfg, db, state, risk):
        importlib.reload(m)
    return cfg, state, risk


@pytest.fixture
def env(tmp_path, monkeypatch):
    """daily=40, total=60 by default. Override per-test by re-setting the env
    var and re-reloading before use."""
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "200")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_DAILY_USD", "40")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_TOTAL_USD", "60")
    return _reload_stack()


def _lose(state, *, market_id, amount, mode="paper"):
    """Open a position of `amount` and close it worthless -> realized -amount."""
    pid = state.open_position(
        mode, market_id=market_id, token_id="t", question="q", category="c",
        side="YES", entry_price=0.5, size_usd=amount, shares=amount * 2,
        fair_prob=0.5, confidence=0.5, thesis="t")
    state.close_position(mode, pid, exit_price=0.0, payout=0.0)


def _win(state, *, market_id, stake, payout, mode="paper"):
    """Open a position of `stake` and close it for `payout` -> realized payout-stake."""
    pid = state.open_position(
        mode, market_id=market_id, token_id="t", question="q", category="c",
        side="YES", entry_price=0.5, size_usd=stake, shares=stake * 2,
        fair_prob=0.5, confidence=0.5, thesis="t")
    state.close_position(mode, pid, exit_price=1.0, payout=payout)


def test_loss_exceeding_both_limits_trips_with_total_reason(env):
    # daily=40, total=60. A single $70 loss exceeds BOTH limits. Since total is
    # checked first, the reason must mention "total".
    _, state, risk = env
    _lose(state, market_id="m", amount=70)
    ok, reason = risk.check_kill_switch("paper")
    assert ok is False
    assert "total" in reason.lower()
    assert "kill switch" in reason.lower()


def test_total_limit_exact_boundary_trips(tmp_path, monkeypatch):
    # Realized loss == total limit should trip (>= comparison). Keep the daily
    # limit high so only the total limit can be the cause.
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "500")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_DAILY_USD", "1000")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_TOTAL_USD", "60")
    _, state, risk = _reload_stack()
    _lose(state, market_id="m", amount=60.0)  # realized -60.00, exactly the cap
    ok, reason = risk.check_kill_switch("paper")
    assert ok is False
    assert "total" in reason.lower()


def test_total_limit_one_cent_under_does_not_trip(tmp_path, monkeypatch):
    # Realized loss one cent under the total limit must NOT trip. Daily kept high.
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "500")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_DAILY_USD", "1000")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_TOTAL_USD", "60")
    _, state, risk = _reload_stack()
    _lose(state, market_id="m", amount=59.99)  # realized -59.99, just under cap
    ok, reason = risk.check_kill_switch("paper")
    assert ok is True
    assert reason == ""


def test_winning_position_recovers_day_pnl_and_does_not_trip(env):
    # daily=40, total=60. Lose $30 then win $40 -> net day/total PnL = +$10.
    # day_loss clamps at 0 (max(0, -(+10))), so the breaker stays untripped.
    _, state, risk = env
    _lose(state, market_id="loss", amount=30)            # realized -30
    _win(state, market_id="win", stake=10, payout=50)    # realized +40
    p = state.get_portfolio("paper")
    assert p["day_realized_pnl"] == 10.0
    assert p["realized_pnl"] == 10.0
    ok, reason = risk.check_kill_switch("paper")
    assert ok is True
    assert reason == ""


def test_day_loss_clamps_at_zero_when_break_even(env):
    # A loss fully offset by a win -> net day PnL == 0 -> day_loss = 0 -> ok.
    _, state, risk = env
    _lose(state, market_id="loss", amount=35)            # realized -35 (over daily=40? no, under)
    _win(state, market_id="win", stake=15, payout=50)    # realized +35 -> net 0
    p = state.get_portfolio("paper")
    assert p["day_realized_pnl"] == 0.0
    ok, reason = risk.check_kill_switch("paper")
    assert ok is True
    assert reason == ""


def test_no_losses_returns_allowed_empty_reason(env):
    _, state, risk = env
    state.get_portfolio("paper")  # initialize the portfolio row
    assert risk.check_kill_switch("paper") == (True, "")


def test_risk_status_exposes_configured_limits_and_untripped_breaker(env):
    _, state, risk = env
    state.get_portfolio("paper")
    status = risk.risk_status("paper")
    assert status["kill_switch_daily_usd"] == 40.0
    assert status["kill_switch_total_usd"] == 60.0
    assert status["breaker_tripped"] is False
    assert status["halt_reason"] == ""


def test_risk_status_reports_tripped_breaker_on_total_loss(env):
    _, state, risk = env
    _lose(state, market_id="m", amount=70)  # exceeds total cap of 60
    status = risk.risk_status("paper")
    assert status["kill_switch_daily_usd"] == 40.0
    assert status["kill_switch_total_usd"] == 60.0
    assert status["breaker_tripped"] is True
    assert "total" in status["halt_reason"].lower()
