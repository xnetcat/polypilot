# tests/test_server_tools.py
"""Breadth coverage of the MCP server surface: that the exact expected set of
tools is registered, that every registered tool resolves to a callable on the
server module, and a handful of response-shape spot-checks against an isolated
paper data dir."""
import importlib

import pytest

# The full, exact tool surface the server is expected to expose.
EXPECTED_TOOLS = {
    "ping",
    "list_markets",
    "get_market",
    "get_order_book",
    "get_price_history",
    "compute_edge",
    "suggest_size",
    "get_risk_status",
    "get_weather_forecast",
    "get_crypto",
    "get_sports",
    "get_economic_data",
    "get_news",
    "place_order",
    "reconcile_positions",
    "close_position",
    "wait_for_next_cycle",
    "get_portfolio",
    "get_positions",
    "get_trade_history",
    "record_thesis",
    "get_similar_markets",
    "record_evaluation",
    "get_performance_stats",
    "get_session_report",
    "get_live_balance",
    "sync_live_positions",
    "preflight_live",
}


@pytest.fixture
def srv(tmp_path, monkeypatch):
    """Reload config/db/state/server against an isolated paper data dir with a
    known starting balance so portfolio/state assertions are deterministic."""
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_MODE", "paper")
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "100")
    import polymarket_mcp.config as cfg
    import polymarket_mcp.db as db
    import polymarket_mcp.state as state
    import polymarket_mcp.server as server
    for m in (cfg, db, state, server):
        importlib.reload(m)
    return server


def _registered_tool_names(server) -> set[str]:
    return {t.name for t in server.mcp._tool_manager.list_tools()}


def test_registered_tool_set_is_exactly_expected(srv):
    """The server must register exactly the 28 expected tools — no more, no
    fewer. A drifted set (renamed/added/removed tool) fails here."""
    names = _registered_tool_names(srv)
    assert names == EXPECTED_TOOLS
    assert len(names) == 28


def test_every_tool_resolves_to_callable_attribute(srv):
    """Each registered tool name must resolve to a callable on the server module
    (FastMCP wraps the module-level function, which stays importable)."""
    for name in EXPECTED_TOOLS:
        fn = getattr(srv, name, None)
        assert fn is not None, f"{name} missing from server module"
        assert callable(fn), f"{name} is not callable"


def test_ping_returns_status_dict(srv):
    assert srv.ping() == {"status": "ok", "service": "polypilot-mcp"}


def test_get_risk_status_shape(srv):
    rs = srv.get_risk_status()
    assert "kill_switch_daily_usd" in rs
    assert "kill_switch_total_usd" in rs
    assert "breaker_tripped" in rs
    # Fresh portfolio: nothing lost yet, breaker must be untripped.
    assert rs["breaker_tripped"] is False


def test_compute_edge_expected_value(srv):
    # EV = max(0, (fair - yes_price) - 0.02 fee) = (0.60 - 0.45) - 0.02 = 0.13
    out = srv.compute_edge(0.60, "YES", 0.45)
    assert out["expected_value"] == 0.13
    assert out["side"] == "YES"


def test_get_portfolio_cash_is_starting_balance(srv):
    # POLYPILOT_STARTING_BALANCE_USD was set to 100 in the fixture.
    assert srv.get_portfolio()["cash"] == 100.0


def test_record_evaluation_then_get_similar_markets_finds_row(srv):
    out = srv.record_evaluation(market_id="m42", question="BTC above 100k EOY",
        category="crypto", keywords="btc bitcoin 100k", decision="buy_yes",
        side="YES", confidence=0.7, fair_prob=0.62, yes_price=0.55, thesis="momentum")
    assert out["status"] == "ok"
    assert isinstance(out["id"], int) and out["id"] > 0

    similar = srv.get_similar_markets(keywords="bitcoin btc", limit=5)["similar"]
    assert len(similar) == 1
    assert similar[0]["market_id"] == "m42"
    assert similar[0]["decision"] == "buy_yes"
