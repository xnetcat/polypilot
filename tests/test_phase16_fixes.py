"""Post-review fixes for Phase 16: graceful duplicate-open rejection and
correct multi-outcome loss backfill."""
import importlib

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "100")
    import polymarket_mcp.config as cfg
    import polymarket_mcp.db as db
    import polymarket_mcp.state as state
    import polymarket_mcp.order_book as ob
    import polymarket_mcp.execution as ex
    import polymarket_mcp.risk as risk
    for m in (cfg, db, state, ob, ex, risk):
        importlib.reload(m)
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
                        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    return state, ex


def test_duplicate_open_rejected_gracefully(env):
    state, ex = env
    first = ex.place_order(mode="paper", market_id="m1", token_id="tA", question="Q",
        category="weather", side="YES", size_usd=10.0, fair_prob=0.6,
        confidence=0.6, thesis="t")
    assert first["status"] == "filled"
    # Second open on the SAME market must be a clean rejection, not an
    # IntegrityError bubbling out of the DB.
    second = ex.place_order(mode="paper", market_id="m1", token_id="tA", question="Q",
        category="weather", side="YES", size_usd=10.0, fair_prob=0.6,
        confidence=0.6, thesis="t")
    assert second["status"] == "rejected"
    assert "already hold an open position" in second["reason"]
    assert len(state.get_positions("paper", "open")) == 1


def test_multi_outcome_loss_backfills_empty_outcome_side(env, monkeypatch):
    state, ex = env
    # Multi-outcome position: side is an outcome label, not YES/NO.
    ex.place_order(mode="paper", market_id="m2", token_id="tAlice", question="Who wins?",
        category="sports", side="Alice", size_usd=10.0, fair_prob=0.4,
        confidence=0.5, thesis="t")
    state.record_evaluation("paper", market_id="m2", question="Who wins?",
        category="sports", keywords="who wins", decision="buy_yes", side="Alice",
        confidence=0.5, fair_prob=0.4, yes_price=0.5, thesis="t", stake_usd=10.0)
    monkeypatch.setattr(ex, "_resolved_price", lambda mid, tok: 0.0)  # the held outcome lost
    summary = ex.reconcile_positions(mode="paper")
    assert summary["settled"] == 1

    import polymarket_mcp.db as db
    conn = db.connect("paper")
    try:
        row = conn.execute("SELECT outcome_side, resolved, pnl FROM market_intelligence "
                           "WHERE market_id='m2'").fetchone()
    finally:
        conn.close()
    assert row["resolved"] == 1
    # Multi-outcome loss: the winning outcome is unknown, so outcome_side is blank
    # (NOT a meaningless "YES").
    assert row["outcome_side"] == ""
    assert row["pnl"] == -10.0


def test_binary_loss_backfills_opposite_outcome_side(env, monkeypatch):
    state, ex = env
    ex.place_order(mode="paper", market_id="m3", token_id="tYes", question="Binary?",
        category="weather", side="YES", size_usd=10.0, fair_prob=0.6,
        confidence=0.5, thesis="t")
    state.record_evaluation("paper", market_id="m3", question="Binary?",
        category="weather", keywords="binary", decision="buy_yes", side="YES",
        confidence=0.5, fair_prob=0.6, yes_price=0.5, thesis="t", stake_usd=10.0)
    monkeypatch.setattr(ex, "_resolved_price", lambda mid, tok: 0.0)  # YES lost
    ex.reconcile_positions(mode="paper")

    import polymarket_mcp.db as db
    conn = db.connect("paper")
    try:
        row = conn.execute("SELECT outcome_side FROM market_intelligence "
                           "WHERE market_id='m3'").fetchone()
    finally:
        conn.close()
    assert row["outcome_side"] == "NO"   # binary: opposite side won
