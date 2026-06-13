# tests/test_memory.py
"""Market-intelligence memory: the loop must accrue a track record by logging
every evaluation, and reconcile must backfill outcomes when markets resolve."""
import pytest


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "100")
    import importlib
    import polymarket_mcp.config as cfg, polymarket_mcp.db as db
    import polymarket_mcp.state as state, polymarket_mcp.execution as ex
    import polymarket_mcp.server as server
    for m in (cfg, db, state, ex, server):
        importlib.reload(m)
    return server


def test_record_evaluation_tool_feeds_get_similar_markets(srv):
    out = srv.record_evaluation(market_id="m1", question="NYC high temp 20C",
        category="weather", keywords="nyc temp 20", decision="buy_no",
        side="NO", confidence=0.8, fair_prob=0.3, yes_price=0.45, thesis="cold")
    assert out["status"] == "ok"
    assert isinstance(out["id"], int) and out["id"] > 0

    similar = srv.get_similar_markets(keywords="temp nyc", limit=5)["similar"]
    assert len(similar) == 1
    assert similar[0]["market_id"] == "m1"
    assert similar[0]["decision"] == "buy_no"


def test_reconcile_backfills_evaluation_outcome(srv, monkeypatch):
    import sqlite3
    import polymarket_mcp.state as state
    import polymarket_mcp.execution as ex
    import polymarket_mcp.db as db

    # Log the evaluation (as the skill loop would after deciding to trade).
    srv.record_evaluation(market_id="m1", question="NYC high temp 20C",
        category="weather", keywords="nyc temp 20", decision="buy_yes",
        side="YES", confidence=0.7, fair_prob=0.6, yes_price=0.50, thesis="warm")

    # Open a position on the same market_id, then settle it via reconcile.
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="NYC high temp 20C", category="weather", side="YES",
        size_usd=20.0, fair_prob=0.6, confidence=0.7, thesis="warm")
    monkeypatch.setattr(ex, "_resolved_price", lambda token_id, side: 1.0)
    summary = ex.reconcile_positions(mode="paper")
    assert summary["settled"] == 1

    # The intelligence row for m1 should now be resolved with outcome + pnl.
    conn = db.connect("paper")
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM market_intelligence WHERE market_id='m1'").fetchone()
    finally:
        conn.close()
    assert row["resolved"] == 1
    assert row["outcome_side"] == "YES"   # YES side won (price -> 1.0)
    assert row["resolution_price"] == 1.0
    assert row["pnl"] is not None
    assert round(row["pnl"], 2) == 20.0   # 40 shares * $1 - $20 cost


def test_update_evaluation_outcome_no_row_returns_false(srv):
    import polymarket_mcp.state as state
    # No evaluation logged for this market — backfill must be a no-op (False),
    # never raising, so reconciliation stays robust.
    assert state.update_evaluation_outcome("paper", "unknown_market",
        outcome_side="NO", resolution_price=0.0, pnl=-10.0) is False
