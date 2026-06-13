# tests/test_portfolio_tools.py
import pytest


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "100")
    import importlib
    import polymarket_mcp.config as cfg, polymarket_mcp.db as db
    import polymarket_mcp.state as state, polymarket_mcp.server as server
    for m in (cfg, db, state, server):
        importlib.reload(m)
    return server


def test_get_portfolio_tool(srv):
    p = srv.get_portfolio()
    assert p["cash"] == 100.0


def test_record_thesis_updates_position(srv):
    import polymarket_mcp.state as state
    pid = state.open_position("paper", market_id="m", token_id="t", question="q",
        category="weather", side="YES", entry_price=0.4, size_usd=10, shares=25,
        fair_prob=None, confidence=None, thesis="")
    out = srv.record_thesis(position_id=pid, thesis="forecast 22C supports YES",
                            confidence=0.75, fair_prob=0.65)
    assert out["status"] == "ok"
    pos = srv.get_positions()["positions"][0]
    assert pos["thesis"] == "forecast 22C supports YES"
    assert pos["confidence"] == 0.75
