# tests/test_execution.py
import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "100")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_TOTAL_USD", "40")
    import importlib
    import polymarket_mcp.config as cfg, polymarket_mcp.db as db
    import polymarket_mcp.state as state, polymarket_mcp.risk as risk
    import polymarket_mcp.execution as ex
    for m in (cfg, db, state, risk, ex):
        importlib.reload(m)
    return state, ex


def test_paper_buy_fills_and_opens_position(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "filled"
    assert res["fill_price"] == 0.50
    assert res["shares"] == 40.0   # 20 / 0.50
    assert state.get_portfolio("paper")["cash"] == 80.0


def test_place_order_blocked_when_breaker_tripped(env, monkeypatch):
    state, ex = env
    # Force a loss beyond the $40 total limit
    pid = state.open_position("paper", market_id="x", token_id="t", question="q",
        category="c", side="YES", entry_price=0.5, size_usd=45, shares=90,
        fair_prob=0.5, confidence=0.5, thesis="t")
    state.close_position("paper", pid, exit_price=0.0, payout=0.0)
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=10.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "rejected"
    assert "kill switch" in res["reason"].lower()


def test_live_fill_uses_clob_client(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "MODE", "live")
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    class FakeClient:
        def create_market_order(self, args): return {"order": "signed"}
        def post_order(self, order, otype):
            return {"status": "matched", "orderID": "OID1",
                    "takingAmount": "40", "makingAmount": "20"}
    monkeypatch.setattr(ex, "_get_clob_client", lambda: FakeClient())
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})

    res = ex.place_order(mode="live", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "filled"
    assert res["order_id"] == "OID1"
    assert res["shares"] == 40.0


def test_reconcile_settles_resolved_yes_win(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    ex.place_order(mode="paper", market_id="m1", token_id="tok", question="Q",
        category="weather", side="YES", size_usd=20.0, fair_prob=0.6,
        confidence=0.7, thesis="t")  # 40 shares @ 0.50
    # Market resolves YES -> token price 1.0
    monkeypatch.setattr(ex, "_resolved_price", lambda token_id, side: 1.0)
    summary = ex.reconcile_positions(mode="paper")
    assert summary["settled"] == 1
    p = state.get_portfolio("paper")
    assert p["open_positions"] == 0
    assert round(p["realized_pnl"], 2) == 20.0   # 40*$1 payout - $20 cost


def test_reconcile_no_resolution_keeps_open(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    ex.place_order(mode="paper", market_id="m1", token_id="tok", question="Q",
        category="weather", side="YES", size_usd=20.0, fair_prob=0.6,
        confidence=0.7, thesis="t")
    monkeypatch.setattr(ex, "_resolved_price", lambda token_id, side: None)
    summary = ex.reconcile_positions(mode="paper")
    assert summary["settled"] == 0
    assert state.get_portfolio("paper")["open_positions"] == 1


def test_paper_insufficient_cash_rejected(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    # Bankroll is $100; ask for $150.
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=150.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "rejected"
    assert "insufficient cash" in res["reason"].lower()
    assert state.get_positions("paper") == []
    assert state.get_portfolio("paper")["cash"] == 100.0


def test_paper_limit_price_rejected(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    # Best ask 0.50 is above our limit of 0.45 -> reject, no fill.
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t", limit_price=0.45)
    assert res["status"] == "rejected"
    assert "limit" in res["reason"].lower()
    assert state.get_positions("paper") == []
    assert state.get_portfolio("paper")["cash"] == 100.0


def test_close_position_paper_sells_at_bid(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok", question="Q",
        category="weather", side="YES", size_usd=20.0, fair_prob=0.6,
        confidence=0.7, thesis="t")  # 40 shares @ 0.50, cash 100 -> 80
    pos_id = res["position_id"]
    # Now sell into the best bid of 0.48 -> payout 40 * 0.48 = 19.2
    closed = ex.close_position(mode="paper", position_id=pos_id)
    assert closed["status"] == "closed"
    assert closed["exit_price"] == 0.48
    p = state.get_portfolio("paper")
    assert p["open_positions"] == 0
    assert p["cash"] == round(80.0 + 40.0 * 0.48, 2)   # 80 + 19.2 = 99.2


def test_live_fill_no_fill_guard(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "MODE", "live")
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    class FakeClient:
        def create_market_order(self, args): return {"order": "signed"}
        def post_order(self, order, otype):
            return {"status": "matched", "orderID": "OID",
                    "takingAmount": "0", "makingAmount": "0"}
    monkeypatch.setattr(ex, "_get_clob_client", lambda: FakeClient())
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})

    res = ex.place_order(mode="live", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "no_fill"
    # No zombie position recorded; cash untouched.
    assert state.get_positions("live") == []
    assert state.get_portfolio("live")["cash"] == 100.0


def test_live_close_sells_via_clob(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "MODE", "live")
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    class FakeClient:
        def __init__(self):
            self.sell_calls = []

        def create_market_order(self, args):
            return {"args": args}

        def post_order(self, order, otype):
            args = order["args"]
            if getattr(args, "side", None) == "SELL":
                self.sell_calls.append(args)
                # Sold 40 shares, received $24 USDC.
                return {"status": "matched", "orderID": "SELL1",
                        "takingAmount": "24", "makingAmount": "40"}
            # BUY: 40 shares for $20.
            return {"status": "matched", "orderID": "BUY1",
                    "takingAmount": "40", "makingAmount": "20"}

        def get_balance_allowance(self, params):
            # Plenty of balance; no downward adjustment.
            return {"balance": str(int(40 * 1e6))}

    fake = FakeClient()
    monkeypatch.setattr(ex, "_get_clob_client", lambda: fake)
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})

    buy = ex.place_order(mode="live", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert buy["status"] == "filled"
    pos_id = buy["position_id"]

    closed = ex.close_position(mode="live", position_id=pos_id)
    assert closed["status"] == "closed"
    # SELL path was exercised on the CLOB.
    assert len(fake.sell_calls) == 1
    assert fake.sell_calls[0].side == "SELL"
    assert fake.sell_calls[0].amount == 40.0
    # exit_price = proceeds / shares_sold = 24 / 40 = 0.60
    assert closed["exit_price"] == 0.60
    assert state.get_positions("live") == []
