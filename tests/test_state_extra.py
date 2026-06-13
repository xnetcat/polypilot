# tests/test_state_extra.py
"""Breadth coverage for polymarket_mcp.state: day rollover, exposure,
trade history, position status filtering, similar-market ranking, thesis
updates, and evaluation-outcome backfill."""
import time

import pytest


@pytest.fixture
def st(tmp_path, monkeypatch):
    """Fresh state/db backed by a temp data dir with a known starting balance.

    Reloading config -> db -> state in order ensures each module picks up the
    patched env (and clears db._INITIALIZED, which is module-level state)."""
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "100")
    import importlib
    import polymarket_mcp.config as cfg
    import polymarket_mcp.db as db
    import polymarket_mcp.state as state
    importlib.reload(cfg)
    importlib.reload(db)
    importlib.reload(state)
    return state


def _open(st, **overrides):
    """Open a position with sensible defaults, overridable per call."""
    kwargs = dict(
        market_id="m1", token_id="t1", question="Will X happen?",
        category="weather", side="YES", entry_price=0.40, size_usd=20.0,
        shares=50.0, fair_prob=0.6, confidence=0.7, thesis="t",
    )
    kwargs.update(overrides)
    return st.open_position("paper", **kwargs)


# --- Day rollover -----------------------------------------------------------

def test_get_portfolio_rolls_day_resetting_day_pnl(st):
    import polymarket_mcp.db as db
    # Initialize the portfolio row (also stamps today's day_date via _roll_day).
    st.get_portfolio("paper")

    # Realize some PnL so total realized_pnl is non-zero and we can confirm it
    # is preserved across the rollover.
    pid = _open(st, size_usd=20.0, shares=50.0)
    st.close_position("paper", pid, exit_price=1.0, payout=50.0)  # +30 realized
    realized_before = st.get_portfolio("paper")["realized_pnl"]
    assert realized_before == 30.0

    # Directly rewind the stored day to a stale date with a non-zero day PnL.
    conn = db.connect("paper")
    try:
        conn.execute(
            "UPDATE portfolio_state SET day_date='2000-01-01', "
            "day_realized_pnl=-50 WHERE id=1")
        conn.commit()
    finally:
        conn.close()

    # get_portfolio -> _roll_day notices the new day and zeroes day PnL, but
    # leaves cumulative realized_pnl untouched.
    p = st.get_portfolio("paper")
    assert p["day_realized_pnl"] == 0.0
    assert p["realized_pnl"] == realized_before


# --- Open exposure across multiple markets ----------------------------------

def test_open_exposure_sums_distinct_market_positions(st):
    _open(st, market_id="m1", token_id="t1", size_usd=20.0, shares=50.0)
    _open(st, market_id="m2", token_id="t2", size_usd=15.0, shares=30.0)
    _open(st, market_id="m3", token_id="t3", size_usd=5.0, shares=10.0)

    p = st.get_portfolio("paper")
    assert p["open_positions"] == 3
    assert p["open_exposure_usd"] == 40.0  # 20 + 15 + 5


# --- Trade history ordering and limit ---------------------------------------

def test_trade_history_most_recent_first_and_limit(st):
    # Each open writes a 'buy' trade; close writes a 'sell' trade.
    p1 = _open(st, market_id="m1", token_id="t1")
    p2 = _open(st, market_id="m2", token_id="t2")
    st.close_position("paper", p1, exit_price=1.0, payout=50.0)

    # Three trades total: buy m1, buy m2, sell m1 (sell is most recent).
    history = st.get_trade_history("paper", limit=50)
    assert [t["action"] for t in history][:1] == ["sell"]
    # Strictly non-increasing timestamps => most-recent first.
    times = [t["created_at"] for t in history]
    assert times == sorted(times, reverse=True)

    # limit is honored.
    assert len(st.get_trade_history("paper", limit=2)) == 2


# --- Position status filtering ----------------------------------------------

def test_get_positions_status_open_vs_closed(st):
    p1 = _open(st, market_id="m1", token_id="t1")
    p2 = _open(st, market_id="m2", token_id="t2")
    st.close_position("paper", p1, exit_price=1.0, payout=50.0)

    open_ids = {p["id"] for p in st.get_positions("paper", status="open")}
    closed_ids = {p["id"] for p in st.get_positions("paper", status="closed")}

    # The closed position appears only under status='closed'.
    assert closed_ids == {p1}
    # The still-open position appears only under status='open'.
    assert open_ids == {p2}


# --- Similar-market ranking --------------------------------------------------

def test_get_similar_markets_ranks_by_overlap_and_limit(st):
    # More shared tokens with the query => ranked higher.
    st.record_evaluation("paper", market_id="m1",
        question="Bitcoin price above target today",
        category="crypto", keywords="bitcoin price target",
        decision="buy_yes", side="YES")
    st.record_evaluation("paper", market_id="m2",
        question="Bitcoin halving event",
        category="crypto", keywords="bitcoin halving",
        decision="skip")
    st.record_evaluation("paper", market_id="m3",
        question="Soccer match outcome",
        category="sports", keywords="soccer match",
        decision="buy_no", side="NO")

    ranked = st.get_similar_markets("paper", "bitcoin price target", limit=5)
    # m1 shares 3 tokens (bitcoin/price/target), m2 shares 1 (bitcoin),
    # m3 shares none.
    assert [r["market_id"] for r in ranked] == ["m1", "m2"]

    # limit caps the number of returned rows.
    assert len(st.get_similar_markets("paper", "bitcoin price target", limit=1)) == 1

    # No overlapping tokens => empty result.
    assert st.get_similar_markets("paper", "completely unrelated zzzqqq") == []


# --- Thesis updates ----------------------------------------------------------

def test_update_thesis_open_position_succeeds(st):
    import polymarket_mcp.db as db
    pid = _open(st, market_id="m1", token_id="t1", thesis="old", confidence=0.5,
                fair_prob=0.5)
    assert st.update_thesis("paper", pid, "new thesis", 0.9, 0.8) is True

    conn = db.connect("paper")
    try:
        row = conn.execute(
            "SELECT thesis, confidence, fair_prob FROM positions WHERE id=?",
            (pid,)).fetchone()
    finally:
        conn.close()
    assert row["thesis"] == "new thesis"
    assert row["confidence"] == 0.9
    assert row["fair_prob"] == 0.8


def test_update_thesis_returns_false_for_missing_position(st):
    assert st.update_thesis("paper", 99999, "x", 0.5, 0.5) is False


def test_update_thesis_returns_false_for_closed_position(st):
    pid = _open(st, market_id="m1", token_id="t1")
    st.close_position("paper", pid, exit_price=1.0, payout=50.0)
    # update_thesis only matches status='open', so a closed position is a no-op.
    assert st.update_thesis("paper", pid, "after close", 0.9, 0.8) is False


# --- Evaluation outcome backfill --------------------------------------------

def test_update_evaluation_outcome_backfills_most_recent_unresolved(st):
    import polymarket_mcp.db as db
    id1 = st.record_evaluation("paper", market_id="m1", question="q1",
        category="crypto", keywords="k", decision="buy_yes", side="YES")
    # Ensure a strictly later evaluated_at so "most recent" is unambiguous.
    time.sleep(0.01)
    id2 = st.record_evaluation("paper", market_id="m1", question="q2",
        category="crypto", keywords="k", decision="buy_no", side="NO")

    assert st.update_evaluation_outcome("paper", "m1", "YES", 1.0, 12.5) is True

    conn = db.connect("paper")
    try:
        rows = {r["id"]: r for r in conn.execute(
            "SELECT id, resolved, outcome_side, resolution_price, pnl "
            "FROM market_intelligence WHERE market_id='m1'").fetchall()}
    finally:
        conn.close()
    # The most-recent unresolved row (id2) is the one that gets resolved.
    assert rows[id2]["resolved"] == 1
    assert rows[id2]["outcome_side"] == "YES"
    assert rows[id2]["resolution_price"] == 1.0
    assert rows[id2]["pnl"] == 12.5
    # The older row stays unresolved.
    assert rows[id1]["resolved"] == 0


def test_update_evaluation_outcome_false_when_no_unresolved_row(st):
    # No evaluation logged for this market at all.
    assert st.update_evaluation_outcome("paper", "ghost", "YES", 1.0, 5.0) is False

    # And once the only evaluation is resolved, a second call finds nothing.
    st.record_evaluation("paper", market_id="m1", question="q", category="c",
        keywords="k", decision="buy_yes", side="YES")
    assert st.update_evaluation_outcome("paper", "m1", "YES", 1.0, 5.0) is True
    assert st.update_evaluation_outcome("paper", "m1", "YES", 1.0, 5.0) is False
