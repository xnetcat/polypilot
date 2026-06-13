import sqlite3
import time
import pytest


@pytest.fixture
def st(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "100")
    import importlib, polymarket_mcp.config as cfg, polymarket_mcp.db as db, polymarket_mcp.state as state
    importlib.reload(cfg); importlib.reload(db); importlib.reload(state)
    return state


def test_portfolio_initializes_with_starting_balance(st):
    p = st.get_portfolio("paper")
    assert p["cash"] == 100.0
    assert p["realized_pnl"] == 0.0
    assert p["open_positions"] == 0


def test_open_position_debits_cash_and_lists(st):
    pos_id = st.open_position("paper", market_id="m1", token_id="t1",
        question="Will X happen?", category="weather", side="YES",
        entry_price=0.40, size_usd=20.0, shares=50.0,
        fair_prob=0.6, confidence=0.7, thesis="forecast supports YES")
    p = st.get_portfolio("paper")
    assert p["cash"] == 80.0
    assert p["open_positions"] == 1
    positions = st.get_positions("paper")
    assert positions[0]["id"] == pos_id
    assert positions[0]["thesis"] == "forecast supports YES"


def test_close_position_credits_payout_and_records_pnl(st):
    pos_id = st.open_position("paper", market_id="m1", token_id="t1",
        question="Q", category="weather", side="YES",
        entry_price=0.40, size_usd=20.0, shares=50.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    # YES wins -> 50 shares pay $1 each = $50 payout
    st.close_position("paper", pos_id, exit_price=1.0, payout=50.0)
    p = st.get_portfolio("paper")
    assert p["cash"] == 80.0 + 50.0
    assert round(p["realized_pnl"], 2) == 30.0   # 50 payout - 20 cost
    assert st.get_positions("paper") == []


def test_record_and_find_similar_intelligence(st):
    st.record_evaluation("paper", market_id="m1", question="NYC high temp 20C",
        category="weather", keywords="nyc temp 20", decision="buy_no",
        side="NO", confidence=0.8, fair_prob=0.3, yes_price=0.45, thesis="cold")
    similar = st.get_similar_markets("paper", "temp nyc", limit=5)
    assert len(similar) == 1
    assert similar[0]["decision"] == "buy_no"


def test_cannot_open_two_positions_on_same_market(st):
    st.open_position("paper", market_id="m1", token_id="t1",
        question="Q", category="weather", side="YES",
        entry_price=0.40, size_usd=20.0, shares=50.0,
        fair_prob=0.6, confidence=0.7, thesis="first")
    with pytest.raises(sqlite3.IntegrityError):
        st.open_position("paper", market_id="m1", token_id="t2",
            question="Q", category="weather", side="NO",
            entry_price=0.55, size_usd=10.0, shares=20.0,
            fair_prob=0.4, confidence=0.6, thesis="second")
