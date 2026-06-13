# tests/test_integration_cycle.py
import importlib
import pytest


@pytest.fixture
def sys_(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "100")
    import polymarket_mcp.config as cfg, polymarket_mcp.db as db
    import polymarket_mcp.state as state, polymarket_mcp.order_book as ob
    import polymarket_mcp.execution as ex, polymarket_mcp.risk as risk
    for m in (cfg, db, state, ob, ex, risk):
        importlib.reload(m)
    return state, ex, ob


def test_full_paper_cycle(sys_, monkeypatch):
    state, ex, ob = sys_
    # 1. paper buy fills at 0.50 -> 40 shares for $20
    monkeypatch.setattr(ob, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Highest temp in Tokyo 22C?", category="weather", side="YES",
        size_usd=20.0, fair_prob=0.62, confidence=0.7, thesis="forecast 22C")
    assert res["status"] == "filled"
    assert state.get_portfolio("paper")["open_positions"] == 1

    # 2. market resolves YES -> reconcile settles, PnL = +$20
    monkeypatch.setattr(ex, "_resolved_price", lambda token_id, side: 1.0)
    summary = ex.reconcile_positions(mode="paper")
    assert summary["settled"] == 1
    p = state.get_portfolio("paper")
    assert p["open_positions"] == 0
    assert round(p["realized_pnl"], 2) == 20.0
    assert p["cash"] == 100.0 + 20.0
