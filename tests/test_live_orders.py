# tests/test_live_orders.py
"""Live order management: get_open_orders / cancel_order (CLOB) and
adopt_live_position / import_live_positions (on-chain reconciliation).

All network/CLOB access is mocked via _get_clob_client or by monkeypatching the
failure-aware live-position fetch. The key invariants:

  * get_open_orders / cancel_order never raise — no key -> error dict, a client
    exception -> error dict, success -> structured result.
  * adopt_live_position records an OPEN live position with NO cash debit and NO
    trade row (it reconciles reality, it does not execute a trade), and is
    idempotent on the one-open-per-market unique index.
  * import_live_positions adopts on-chain-only positions and skips ones already
    tracked locally.
"""
import importlib

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "100")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_TOTAL_USD", "40")
    import polymarket_mcp.config as cfg
    import polymarket_mcp.db as db
    import polymarket_mcp.state as state
    import polymarket_mcp.risk as risk
    import polymarket_mcp.execution as ex
    for m in (cfg, db, state, risk, ex):
        importlib.reload(m)
    return state, ex


# --------------------------------------------------------------------------- #
# get_open_orders
# --------------------------------------------------------------------------- #
def test_get_open_orders_no_key(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "")
    res = ex.get_open_orders()
    assert "error" in res
    assert "PRIVATE_KEY" in res["error"]


def test_get_open_orders_success_maps_fields(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    class FakeClient:
        def get_open_orders(self):
            return [
                {"id": "OID1", "market": "M1", "asset_id": "tokA", "side": "BUY",
                 "price": "0.45", "original_size": "100", "size_matched": "20",
                 "outcome": "Yes", "order_type": "GTC", "status": "LIVE"},
                # Alternate field names exercise the defensive mapping.
                {"orderID": "OID2", "market": "M2", "token_id": "tokB",
                 "side": "SELL", "price": "0.60", "size": "50", "outcome": "No"},
            ]

    monkeypatch.setattr(ex, "_get_clob_client", lambda: FakeClient())
    res = ex.get_open_orders()
    assert "error" not in res
    orders = res["orders"]
    assert len(orders) == 2
    o1 = orders[0]
    assert o1["order_id"] == "OID1"
    assert o1["market"] == "M1"
    assert o1["token_id"] == "tokA"
    assert o1["side"] == "BUY"
    assert o1["price"] == "0.45"
    assert o1["size"] == "100"
    assert o1["order_type"] == "GTC"
    # Second order uses the fallback key names.
    o2 = orders[1]
    assert o2["order_id"] == "OID2"
    assert o2["token_id"] == "tokB"
    assert o2["size"] == "50"


def test_get_open_orders_empty_returns_empty_list(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    class FakeClient:
        def get_open_orders(self):
            return []  # no resting orders — success, not an error

    monkeypatch.setattr(ex, "_get_clob_client", lambda: FakeClient())
    res = ex.get_open_orders()
    assert res == {"orders": []}


def test_get_open_orders_client_exception_is_error_not_raise(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    class FakeClient:
        def get_open_orders(self):
            raise RuntimeError("clob down")

    monkeypatch.setattr(ex, "_get_clob_client", lambda: FakeClient())
    res = ex.get_open_orders()  # must NOT raise
    assert "error" in res
    assert "clob down" in res["error"]


def test_get_open_orders_client_build_failure_is_error(env, monkeypatch):
    # Even the _get_clob_client construction blowing up is caught.
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    def _boom():
        raise RuntimeError("bad creds")

    monkeypatch.setattr(ex, "_get_clob_client", _boom)
    res = ex.get_open_orders()
    assert "error" in res
    assert "bad creds" in res["error"]


# --------------------------------------------------------------------------- #
# cancel_order
# --------------------------------------------------------------------------- #
def test_cancel_order_no_key(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "")
    res = ex.cancel_order("OID1")
    assert res["status"] == "error"
    assert "PRIVATE_KEY" in res["reason"]


def test_cancel_order_blank_id_rejected(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    res = ex.cancel_order("   ")
    assert res["status"] == "error"
    assert "order_id" in res["reason"]


def test_cancel_order_success(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    calls = []

    class FakeClient:
        def cancel_order(self, payload):
            calls.append(payload.orderID)
            return {"canceled": [payload.orderID], "not_canceled": {}}

    monkeypatch.setattr(ex, "_get_clob_client", lambda: FakeClient())
    res = ex.cancel_order("OID1")
    assert res["status"] == "cancelled"
    assert res["order_id"] == "OID1"
    assert calls == ["OID1"]
    # An empty not_canceled map is not surfaced.
    assert "not_canceled" not in res


def test_cancel_order_surfaces_not_canceled_detail(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    class FakeClient:
        def cancel_order(self, payload):
            # Order already filled -> CLOB reports it couldn't be cancelled.
            return {"canceled": [], "not_canceled": {payload.orderID: "already matched"}}

    monkeypatch.setattr(ex, "_get_clob_client", lambda: FakeClient())
    res = ex.cancel_order("OID1")
    assert res["status"] == "cancelled"
    assert res["not_canceled"] == {"OID1": "already matched"}


def test_cancel_order_client_exception_is_error_not_raise(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    class FakeClient:
        def cancel_order(self, payload):
            raise RuntimeError("cancel failed")

    monkeypatch.setattr(ex, "_get_clob_client", lambda: FakeClient())
    res = ex.cancel_order("OID1")  # must NOT raise
    assert res["status"] == "error"
    assert "cancel failed" in res["reason"]


# --------------------------------------------------------------------------- #
# state.adopt_live_position
# --------------------------------------------------------------------------- #
def test_adopt_live_position_no_cash_debit_no_trade_row(env):
    state, _ = env
    cash_before = state.get_portfolio("live")["cash"]
    trades_before = len(state.get_trade_history("live", limit=100))

    pid = state.adopt_live_position("live", market_id="M1", token_id="tokA",
        question="Adopted YES", category="", side="Yes", entry_price=0.40,
        size_usd=20.0, shares=50.0, thesis="adopted")
    assert pid is not None

    # An open position exists with the adopted shares.
    open_pos = state.get_positions("live", status="open")
    assert len(open_pos) == 1
    assert open_pos[0]["market_id"] == "M1"
    assert open_pos[0]["shares"] == 50.0
    assert open_pos[0]["side"] == "Yes"

    p = state.get_portfolio("live")
    # Crucially: cash is NOT debited and trade_count is NOT incremented.
    assert p["cash"] == cash_before
    assert p["trade_count"] == 0
    assert p["open_positions"] == 1
    # No 'buy' trade row was logged for the adoption.
    assert len(state.get_trade_history("live", limit=100)) == trades_before


def test_adopt_live_position_idempotent_on_readopt(env):
    state, _ = env
    pid1 = state.adopt_live_position("live", market_id="M1", token_id="tokA",
        question="q", category="", side="Yes", entry_price=0.40,
        size_usd=20.0, shares=50.0)
    # Re-adopt the SAME market with updated shares/entry -> UPDATE in place,
    # no duplicate row, no IntegrityError on the one-open-per-market index.
    pid2 = state.adopt_live_position("live", market_id="M1", token_id="tokA",
        question="q", category="", side="Yes", entry_price=0.42,
        size_usd=31.5, shares=75.0)
    assert pid1 == pid2

    open_pos = state.get_positions("live", status="open")
    assert len(open_pos) == 1  # still exactly one open position
    assert open_pos[0]["shares"] == 75.0       # updated
    assert open_pos[0]["entry_price"] == 0.42  # updated
    assert open_pos[0]["size_usd"] == 31.5

    # Still no cash movement / trades across both adoptions.
    p = state.get_portfolio("live")
    assert p["cash"] == 100.0
    assert p["trade_count"] == 0


def test_adopt_live_position_distinct_markets_coexist(env):
    state, _ = env
    state.adopt_live_position("live", market_id="M1", token_id="tA", question="q",
        category="", side="Yes", entry_price=0.4, size_usd=20.0, shares=50.0)
    state.adopt_live_position("live", market_id="M2", token_id="tB", question="q",
        category="", side="No", entry_price=0.6, size_usd=12.0, shares=20.0)
    open_pos = state.get_positions("live", status="open")
    assert {p["market_id"] for p in open_pos} == {"M1", "M2"}
    assert state.get_portfolio("live")["cash"] == 100.0


# --------------------------------------------------------------------------- #
# import_live_positions
# --------------------------------------------------------------------------- #
def test_import_live_positions_no_key(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "")
    res = ex.import_live_positions(mode="live")
    assert "error" in res
    assert res["adopted"] == 0


def test_import_live_positions_adopts_chain_only_skips_tracked(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    # Already tracked locally: market "BOTH" (a normal open, debits cash).
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    state.open_position("live", market_id="BOTH", token_id="tB", question="q",
        category="c", side="YES", entry_price=0.5, size_usd=10.0, shares=20.0,
        fair_prob=0.5, confidence=0.5, thesis="t")
    cash_after_open = state.get_portfolio("live")["cash"]  # 90.0 (debited)

    # On-chain fetch (failure-aware) returns: BOTH (already tracked -> skip) and
    # CHAIN1 (chain-only -> adopt).
    monkeypatch.setattr(ex, "_fetch_live_positions", lambda: ([
        {"market_id": "BOTH", "outcome": "Yes", "shares": 20.0, "avg_price": 0.5,
         "cost": 10.0, "asset_id": "tB"},
        {"market_id": "CHAIN1", "outcome": "Yes", "shares": 5.0, "avg_price": 0.30,
         "cost": 1.5, "asset_id": "tC1"},
    ], None))

    res = ex.import_live_positions(mode="live")
    assert res["adopted"] == 1
    assert res["skipped"] == 1
    assert [d["market_id"] for d in res["details"]] == ["CHAIN1"]

    # Now two open positions: the pre-existing BOTH and the adopted CHAIN1.
    open_markets = {p["market_id"] for p in state.get_positions("live", status="open")}
    assert open_markets == {"BOTH", "CHAIN1"}

    chain = [p for p in state.get_positions("live", status="open")
             if p["market_id"] == "CHAIN1"][0]
    assert chain["shares"] == 5.0
    assert chain["entry_price"] == 0.30
    assert chain["size_usd"] == 1.5

    # The adoption did NOT debit cash (only the earlier real open did) and did
    # NOT log a buy trade for CHAIN1.
    p = state.get_portfolio("live")
    assert p["cash"] == cash_after_open  # unchanged by the adoption
    # trade_count reflects only the single real open_position, not the adoption.
    assert p["trade_count"] == 1


def test_import_live_positions_idempotent_second_run_skips_all(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    monkeypatch.setattr(ex, "_fetch_live_positions", lambda: ([
        {"market_id": "CHAIN1", "outcome": "Yes", "shares": 5.0, "avg_price": 0.30,
         "cost": 1.5, "asset_id": "tC1"},
    ], None))

    first = ex.import_live_positions(mode="live")
    assert first["adopted"] == 1 and first["skipped"] == 0
    # Second run: CHAIN1 is now tracked locally -> skipped, no duplicate.
    second = ex.import_live_positions(mode="live")
    assert second["adopted"] == 0 and second["skipped"] == 1
    assert len(state.get_positions("live", status="open")) == 1


def test_import_live_positions_fetch_failure_adopts_nothing(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    monkeypatch.setattr(ex, "_fetch_live_positions",
        lambda: ([], "RuntimeError: chain unreachable"))
    res = ex.import_live_positions(mode="live")
    assert res["adopted"] == 0
    assert "fetch_error" in res
    assert state.get_positions("live", status="open") == []


def test_import_live_positions_empty_chain(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    monkeypatch.setattr(ex, "_fetch_live_positions", lambda: ([], None))
    res = ex.import_live_positions(mode="live")
    assert res["adopted"] == 0
    assert res["skipped"] == 0
    assert state.get_positions("live", status="open") == []


# --------------------------------------------------------------------------- #
# get_live_account — authoritative Polymarket-sourced snapshot
# --------------------------------------------------------------------------- #
def test_get_live_account_no_key(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "")
    res = ex.get_live_account()
    assert res["status"] == "error"
    assert "PRIVATE_KEY" in res["reason"]


def test_get_live_account_marks_positions_and_aggregates(env, monkeypatch):
    # All network mocked: a real balance, two on-chain positions (both markable),
    # and live midpoints. equity = balance + Σ(shares*mid); unrealized =
    # Σ(mkt_value - cost).
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    monkeypatch.setattr(ex, "get_live_balance", lambda: {"balance_usd": 250.0})
    monkeypatch.setattr(ex, "get_live_positions", lambda: [
        {"market_id": "M1", "outcome": "Yes", "shares": 40.0, "avg_price": 0.50,
         "cost": 20.0, "asset_id": "tokA"},
        {"market_id": "M2", "outcome": "No", "shares": 20.0, "avg_price": 0.30,
         "cost": 6.0, "asset_id": "tokB"},
    ])
    mids = {"tokA": 0.60, "tokB": 0.25}
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"midpoint": mids[tid]})

    res = ex.get_live_account()
    assert res["source"] == "polymarket"
    assert res["balance_usd"] == 250.0
    # Position values: 40*0.60 = 24.0 ; 20*0.25 = 5.0 -> total 29.0.
    assert res["total_position_value"] == 29.0
    assert res["total_cost"] == 26.0  # 20 + 6
    # Unrealized: (24-20) + (5-6) = 4 + (-1) = 3.0.
    assert res["unrealized_pnl"] == 3.0
    # Equity = balance + marked position value.
    assert res["equity"] == 279.0  # 250 + 29
    assert res["marked"] == 2
    assert res["unmarked"] == 0
    # Per-position marking is reflected in each entry.
    p1 = next(p for p in res["positions"] if p["market_id"] == "M1")
    assert p1["current_price"] == 0.60
    assert p1["market_value"] == 24.0
    assert p1["unrealized_pnl"] == 4.0


def test_get_live_account_unmarked_position_best_effort(env, monkeypatch):
    # One markable position, one with no usable midpoint: the unmarked one is
    # still INCLUDED (null marks) and counted, not fatal; aggregates use only the
    # marked one.
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    monkeypatch.setattr(ex, "get_live_balance", lambda: {"balance_usd": 100.0})
    monkeypatch.setattr(ex, "get_live_positions", lambda: [
        {"market_id": "GOOD", "outcome": "Yes", "shares": 10.0, "avg_price": 0.40,
         "cost": 4.0, "asset_id": "tokG"},
        {"market_id": "BAD", "outcome": "Yes", "shares": 50.0, "avg_price": 0.20,
         "cost": 10.0, "asset_id": "tokX"},
    ])

    def fake_book(tid):
        if tid == "tokG":
            return {"midpoint": 0.50}
        return {"error": "one-sided book", "midpoint": 0.0}  # no usable mid

    monkeypatch.setattr(ex.order_book, "fetch_order_book", fake_book)
    res = ex.get_live_account()
    assert res["marked"] == 1
    assert res["unmarked"] == 1
    # Only GOOD is marked: 10 * 0.50 = 5.0 value, unrealized 5.0 - 4.0 = 1.0.
    assert res["total_position_value"] == 5.0
    assert res["unrealized_pnl"] == 1.0
    # total_cost still sums BOTH positions' cost (4 + 10).
    assert res["total_cost"] == 14.0
    assert res["equity"] == 105.0  # 100 + 5
    # The unmarked BAD position is present with null marks.
    bad = next(p for p in res["positions"] if p["market_id"] == "BAD")
    assert bad["current_price"] is None
    assert bad["market_value"] is None
    assert bad["unrealized_pnl"] is None
    assert bad["shares"] == 50.0


def test_get_live_account_balance_unreadable_is_error(env, monkeypatch):
    # Key set but the balance read fails -> error/unavailable shape, never raises,
    # and positions are NOT fetched/marked.
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    monkeypatch.setattr(ex, "get_live_balance",
        lambda: {"error": "RuntimeError: clob down", "balance_usd": None})
    monkeypatch.setattr(ex, "get_live_positions",
        lambda: pytest.fail("positions must not be fetched when balance fails"))
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: pytest.fail("no marking when balance fails"))
    res = ex.get_live_account()
    assert res["status"] == "error"
    assert "clob down" in res["reason"]


def test_get_live_account_empty_positions(env, monkeypatch):
    # Flat live account: real balance, no positions -> equity == balance, zero
    # aggregates, empty positions list.
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    monkeypatch.setattr(ex, "get_live_balance", lambda: {"balance_usd": 75.0})
    monkeypatch.setattr(ex, "get_live_positions", lambda: [])
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: pytest.fail("no marking with zero positions"))
    res = ex.get_live_account()
    assert res["source"] == "polymarket"
    assert res["balance_usd"] == 75.0
    assert res["equity"] == 75.0
    assert res["total_position_value"] == 0.0
    assert res["total_cost"] == 0.0
    assert res["unrealized_pnl"] == 0.0
    assert res["positions"] == []
    assert res["marked"] == 0
    assert res["unmarked"] == 0
