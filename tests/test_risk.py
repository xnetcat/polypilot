# tests/test_risk.py
import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "100")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_DAILY_USD", "30")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_TOTAL_USD", "60")
    import importlib
    import polymarket_mcp.config as cfg, polymarket_mcp.db as db
    import polymarket_mcp.state as state, polymarket_mcp.risk as risk
    for m in (cfg, db, state, risk):
        importlib.reload(m)
    return state, risk


def test_breaker_ok_when_no_losses(env):
    state, risk = env
    state.get_portfolio("paper")  # initialize
    ok, reason = risk.check_kill_switch("paper")
    assert ok is True and reason == ""


def test_breaker_trips_on_total_loss(env):
    state, risk = env
    # Open a $50 position then close it worthless -> -$50 realized.
    # $50 loss exceeds neither cap alone? It exceeds the daily cap ($30) and is
    # below the total cap ($60); since the same close hits both realized and
    # day_realized PnL, the breaker must trip. (Daily is the binding limit here.)
    pid = state.open_position("paper", market_id="m", token_id="t", question="q",
        category="c", side="YES", entry_price=0.5, size_usd=50, shares=100,
        fair_prob=0.5, confidence=0.5, thesis="t")
    state.close_position("paper", pid, exit_price=0.0, payout=0.0)
    ok, reason = risk.check_kill_switch("paper")
    assert ok is False
    assert "kill switch" in reason.lower()
    assert "loss" in reason.lower()


def test_breaker_trips_on_total_loss_above_total_cap(env):
    state, risk = env
    # Two $50 positions closed worthless -> -$100 realized, which exceeds the
    # $60 total cap and is reported as a total-loss kill-switch.
    for mkt in ("m1", "m2"):
        pid = state.open_position("paper", market_id=mkt, token_id="t", question="q",
            category="c", side="YES", entry_price=0.5, size_usd=50, shares=100,
            fair_prob=0.5, confidence=0.5, thesis="t")
        state.close_position("paper", pid, exit_price=0.0, payout=0.0)
    ok, reason = risk.check_kill_switch("paper")
    assert ok is False
    assert "total loss" in reason.lower()


def test_status_reports_limits(env):
    state, risk = env
    state.get_portfolio("paper")
    status = risk.risk_status("paper")
    assert status["kill_switch_total_usd"] == 60.0
    assert status["breaker_tripped"] is False
