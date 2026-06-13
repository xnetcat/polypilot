"""Long multi-cycle paper-trading session — proves the full pipeline's money
math is correct end to end.

Network is mocked deterministically: order books drive fills/closes, and a
controllable resolver decides which markets have *finished* (closed) and how.
The core invariant asserted everywhere is conservation of money:

    cash + open_cost_basis  ==  starting_balance + realized_pnl

i.e. every dollar is either still in cash, locked in an open position's cost
basis, or accounted for as realized PnL. If that ever breaks, the ledger lies.
"""
import importlib

import pytest


@pytest.fixture
def sim(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "100")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_DAILY_USD", "30")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_TOTAL_USD", "50")
    import polymarket_mcp.config as cfg
    import polymarket_mcp.db as db
    import polymarket_mcp.state as state
    import polymarket_mcp.order_book as ob
    import polymarket_mcp.execution as ex
    import polymarket_mcp.risk as risk
    for m in (cfg, db, state, ob, ex, risk):
        importlib.reload(m)

    books: dict[str, dict] = {}
    resolutions: dict[str, float | None] = {}

    monkeypatch.setattr(ex.order_book, "fetch_order_book",
                        lambda token_id: books.get(token_id,
                                                   {"best_ask": 0.50, "best_bid": 0.48}))
    monkeypatch.setattr(ex, "_resolved_price",
                        lambda market_id, token_id: resolutions.get(market_id))
    return state, ex, risk, books, resolutions


def _conserved(state):
    p = state.get_portfolio("paper")
    lhs = round(p["cash"] + p["open_exposure_usd"], 2)
    rhs = round(p["starting_balance"] + p["realized_pnl"], 2)
    assert lhs == rhs, f"ledger broken: cash+exposure={lhs} != start+pnl={rhs} ({p})"
    return p


def _buy(state, ex, books, market_id, token, ask, size, side="YES", cat="weather"):
    books[token] = {"best_ask": ask, "best_bid": round(ask - 0.02, 4)}
    res = ex.place_order(mode="paper", market_id=market_id, token_id=token,
                         question=f"Q {market_id}", category=cat, side=side,
                         size_usd=size, fair_prob=0.6, confidence=0.6, thesis="t")
    return res


def test_long_paper_session_accounting(sim):
    state, ex, risk, books, resolutions = sim

    # ---- Cycle 1: open three positions across the bankroll ----
    r1 = _buy(state, ex, books, "A", "tA", 0.50, 20.0)   # 40 shares @ .50
    r2 = _buy(state, ex, books, "B", "tB", 0.25, 10.0)   # 40 shares @ .25
    r3 = _buy(state, ex, books, "C", "tC", 0.40, 8.0)    # 20 shares @ .40
    assert [r["status"] for r in (r1, r2, r3)] == ["filled"] * 3
    assert r1["shares"] == 40.0 and r2["shares"] == 40.0 and r3["shares"] == 20.0

    p = _conserved(state)
    assert p["open_positions"] == 3
    assert p["cash"] == 100 - 20 - 10 - 8   # 62
    assert p["open_exposure_usd"] == 38.0

    # log an evaluation per market (memory / track record)
    for mid in ("A", "B", "C"):
        state.record_evaluation("paper", market_id=mid, question=f"Q {mid}",
                                category="weather", keywords=f"market {mid.lower()}",
                                decision="buy_yes", side="YES", confidence=0.6,
                                fair_prob=0.6, yes_price=0.5, thesis="t")

    # ---- A finishes a winner, B finishes a loser, C still open ----
    resolutions["A"] = 1.0
    resolutions["B"] = 0.0
    summary = ex.reconcile_positions(mode="paper")
    assert summary["settled"] == 2
    results = {d["question"]: d["result"] for d in summary["details"]}
    assert results["Q A"] == "won" and results["Q B"] == "lost"

    p = _conserved(state)
    assert p["open_positions"] == 1                  # only C remains
    # A: 40*$1 − $20 = +$20 ; B: $0 − $10 = −$10  → net +$10
    assert p["realized_pnl"] == 10.0
    assert p["cash"] == 62 + 40 + 0                  # 102 (A paid out 40, B nothing)

    # memory: A/B backfilled with outcomes; C still unresolved
    import polymarket_mcp.db as db
    conn = db.connect("paper")
    try:
        rowA = conn.execute("SELECT resolved, outcome_side, pnl FROM market_intelligence "
                            "WHERE market_id='A'").fetchone()
        rowB = conn.execute("SELECT resolved, pnl FROM market_intelligence "
                            "WHERE market_id='B'").fetchone()
        rowC = conn.execute("SELECT resolved FROM market_intelligence "
                            "WHERE market_id='C'").fetchone()
    finally:
        conn.close()
    assert rowA["resolved"] == 1 and rowA["outcome_side"] == "YES" and rowA["pnl"] == 20.0
    assert rowB["resolved"] == 1 and rowB["pnl"] == -10.0
    assert rowC["resolved"] == 0

    # ---- Capital recycling: C ran up, close it early at the bid ----
    cpos = state.get_positions("paper", "open")[0]
    books[cpos["token_id"]] = {"best_ask": 0.96, "best_bid": 0.95}
    closed = ex.close_position(mode="paper", position_id=cpos["id"])
    assert closed["status"] == "closed" and closed["exit_price"] == 0.95

    p = _conserved(state)
    assert p["open_positions"] == 0
    # C: 20 shares * $0.95 − $8 cost = +$11  → realized 10 + 11 = 21
    assert p["realized_pnl"] == 21.0
    assert p["cash"] == 102 + 19                      # 121

    # full round-trip: every dollar accounted for, no positions left
    assert state.get_positions("paper", "open") == []
    assert len(state.get_positions("paper", "closed")) == 3


def test_open_positions_do_not_trip_kill_switch(sim):
    # The kill-switch is REALIZED-PnL based. Large open (unrealized) positions —
    # even ones currently underwater — must NOT trip it; only finished losses do.
    state, ex, risk, books, resolutions = sim
    _buy(state, ex, books, "X", "tX", 0.90, 45.0)     # big open position
    status = risk.risk_status("paper")
    assert status["breaker_tripped"] is False
    ok, _ = risk.check_kill_switch("paper")
    assert ok is True
    _conserved(state)


def test_daily_kill_switch_halts_new_orders(sim):
    state, ex, risk, books, resolutions = sim
    # Two finished losers of $16 each = −$32 realized today (daily limit $30).
    _buy(state, ex, books, "D", "tD", 0.40, 16.0)
    _buy(state, ex, books, "E", "tE", 0.40, 16.0)
    resolutions["D"] = 0.0
    resolutions["E"] = 0.0
    ex.reconcile_positions(mode="paper")

    p = _conserved(state)
    assert p["realized_pnl"] == -32.0
    assert p["day_realized_pnl"] == -32.0

    status = risk.risk_status("paper")
    assert status["breaker_tripped"] is True
    assert "daily" in status["halt_reason"].lower()

    # New orders are refused while the breaker is tripped.
    blocked = _buy(state, ex, books, "F", "tF", 0.50, 5.0)
    assert blocked["status"] == "rejected"
    assert "kill switch" in blocked["reason"].lower()
    # ...and no phantom position/cash movement from the rejected order.
    assert state.get_positions("paper", "open") == []
    _conserved(state)


def test_total_kill_switch_halts_new_orders(tmp_path, monkeypatch):
    # Daily very high, total low: a slow bleed across "days" trips TOTAL.
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "200")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_DAILY_USD", "1000")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_TOTAL_USD", "50")
    import polymarket_mcp.config as cfg
    import polymarket_mcp.db as db
    import polymarket_mcp.state as state
    import polymarket_mcp.order_book as ob
    import polymarket_mcp.execution as ex
    import polymarket_mcp.risk as risk
    for m in (cfg, db, state, ob, ex, risk):
        importlib.reload(m)
    books = {}
    resolutions = {}
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
                        lambda t: books.get(t, {"best_ask": 0.5, "best_bid": 0.48}))
    monkeypatch.setattr(ex, "_resolved_price", lambda m, t: resolutions.get(m))

    _buy(state, ex, books, "G", "tG", 0.40, 30.0)
    _buy(state, ex, books, "H", "tH", 0.40, 25.0)
    resolutions["G"] = 0.0   # -30
    resolutions["H"] = 0.0   # -25  → total -55 > 50 cap
    ex.reconcile_positions(mode="paper")

    status = risk.risk_status("paper")
    assert status["breaker_tripped"] is True
    assert "total" in status["halt_reason"].lower()
    blocked = _buy(state, ex, books, "I", "tI", 0.50, 5.0)
    assert blocked["status"] == "rejected" and "kill switch" in blocked["reason"].lower()


def test_reconcile_still_settles_after_breaker_tripped(sim):
    # Once tripped, NEW orders are blocked but already-open positions must still
    # be able to FINISH (reconcile must keep settling) so the book closes out.
    state, ex, risk, books, resolutions = sim
    _buy(state, ex, books, "J", "tJ", 0.40, 16.0)
    _buy(state, ex, books, "K", "tK", 0.40, 16.0)
    _buy(state, ex, books, "L", "tL", 0.50, 6.0)     # opened before breaker trips
    resolutions["J"] = 0.0
    resolutions["K"] = 0.0
    ex.reconcile_positions(mode="paper")             # -32 → daily breaker trips
    assert risk.risk_status("paper")["breaker_tripped"] is True

    # L is still open; a new buy is blocked, but L can still finish.
    assert _buy(state, ex, books, "M", "tM", 0.5, 1.0)["status"] == "rejected"
    resolutions["L"] = 1.0                            # L wins: 12 shares * $1 = 12, cost 6 → +6
    summary = ex.reconcile_positions(mode="paper")
    assert summary["settled"] == 1
    p = _conserved(state)
    assert p["realized_pnl"] == -32.0 + 6.0          # -26
    assert state.get_positions("paper", "open") == []
