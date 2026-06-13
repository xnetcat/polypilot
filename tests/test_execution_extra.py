# tests/test_execution_extra.py
"""Additional breadth coverage for execution.py — complements
tests/test_execution.py without duplicating its cases.

Covers: missing-ask liquidity, zero/negative size, close on a nonexistent
position, close with no bid, reconcile with mixed outcomes, live no-liquidity
buy, and a live sell with zero proceeds (phantom-loss guard)."""
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


# --------------------------------------------------------------------------- #
# Paper: missing-ask liquidity
# --------------------------------------------------------------------------- #
def test_paper_buy_no_ask_liquidity_rejected(env, monkeypatch):
    state, ex = env
    # Live book has a bid but no ask -> nothing to buy into.
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": None, "best_bid": 0.48})
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "rejected"
    assert "liquidity" in res["reason"].lower()
    # No position opened, cash untouched.
    assert state.get_positions("paper") == []
    assert state.get_portfolio("paper")["cash"] == 100.0


# --------------------------------------------------------------------------- #
# Zero / negative size
# --------------------------------------------------------------------------- #
def test_paper_buy_zero_size_rejected(env, monkeypatch):
    state, ex = env
    # Book is healthy; the size guard must reject before any fill.
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "rejected"
    assert "positive" in res["reason"].lower()
    assert state.get_positions("paper") == []
    assert state.get_portfolio("paper")["cash"] == 100.0


def test_paper_buy_negative_size_rejected(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=-10.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "rejected"
    assert "positive" in res["reason"].lower()
    assert state.get_positions("paper") == []
    assert state.get_portfolio("paper")["cash"] == 100.0


# --------------------------------------------------------------------------- #
# close_position: nonexistent id
# --------------------------------------------------------------------------- #
def test_close_position_nonexistent_id_rejected(env, monkeypatch):
    state, ex = env
    # No book lookup should even be needed; guard hits first.
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    res = ex.close_position(mode="paper", position_id=99999)
    assert res["status"] == "rejected"
    assert "not open" in res["reason"].lower()
    # No ledger change.
    p = state.get_portfolio("paper")
    assert p["cash"] == 100.0
    assert p["open_positions"] == 0
    assert p["trade_count"] == 0


# --------------------------------------------------------------------------- #
# close_position: no bid liquidity to sell into
# --------------------------------------------------------------------------- #
def test_close_position_paper_no_bid_rejected(env, monkeypatch):
    state, ex = env
    # Open against a healthy book first.
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "filled"
    pos_id = res["position_id"]
    cash_after_open = state.get_portfolio("paper")["cash"]  # 80.0

    # Bid disappears -> nothing to sell into.
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.52, "best_bid": None})
    closed = ex.close_position(mode="paper", position_id=pos_id)
    assert closed["status"] == "rejected"
    assert "no bid" in closed["reason"].lower()
    # Position stays open; cash unchanged from the post-open balance.
    p = state.get_portfolio("paper")
    assert p["open_positions"] == 1
    assert p["cash"] == cash_after_open
    assert len(state.get_positions("paper", status="open")) == 1


# --------------------------------------------------------------------------- #
# reconcile: mixed outcomes (one win, one loss, one unresolved)
# --------------------------------------------------------------------------- #
def test_reconcile_mixed_outcomes(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    # Three positions, each $20 -> 40 shares @ 0.50. Distinct market_ids so we
    # can resolve each independently.
    for mid in ("win", "loss", "pending"):
        r = ex.place_order(mode="paper", market_id=mid, token_id=f"tok_{mid}",
            question=f"Q-{mid}", category="weather", side="YES", size_usd=20.0,
            fair_prob=0.6, confidence=0.7, thesis="t")
        assert r["status"] == "filled"

    assert state.get_portfolio("paper")["open_positions"] == 3

    # reconcile_positions calls _resolved_price(market_id, token_id) positionally.
    resolution = {"win": 1.0, "loss": 0.0, "pending": None}
    monkeypatch.setattr(ex, "_resolved_price",
        lambda market_id, token_id: resolution[market_id])

    summary = ex.reconcile_positions(mode="paper")
    assert summary["settled"] == 2

    p = state.get_portfolio("paper")
    assert p["open_positions"] == 1  # 'pending' stays open
    # win:  40*1.0 - 20 = +20 ; loss: 40*0.0 - 20 = -20 -> net 0.0
    win_pnl = 40.0 * 1.0 - 20.0
    loss_pnl = 40.0 * 0.0 - 20.0
    assert round(p["realized_pnl"], 2) == round(win_pnl + loss_pnl, 2) == 0.0

    # The surviving open position is the unresolved one.
    open_left = state.get_positions("paper", status="open")
    assert len(open_left) == 1
    assert open_left[0]["market_id"] == "pending"

    # Detail rows reflect won/lost classification.
    results = sorted(d["result"] for d in summary["details"])
    assert results == ["lost", "won"]


# --------------------------------------------------------------------------- #
# Live buy: no liquidity ("no orders found to match")
# --------------------------------------------------------------------------- #
def test_live_buy_no_liquidity_rejected(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "MODE", "live")
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    class FakeClient:
        def create_market_order(self, args):
            return {"order": "signed"}

        def post_order(self, order, otype):
            raise Exception("could not insert FAK order: no orders found to match")

    monkeypatch.setattr(ex, "_get_clob_client", lambda: FakeClient())
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})

    res = ex.place_order(mode="live", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "rejected"
    assert "liquidity" in res["reason"].lower()
    # No position recorded; cash untouched.
    assert state.get_positions("live") == []
    assert state.get_portfolio("live")["cash"] == 100.0


# --------------------------------------------------------------------------- #
# Live sell: zero proceeds must NOT book a phantom total loss
# --------------------------------------------------------------------------- #
def test_live_close_zero_proceeds_keeps_position_open(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "MODE", "live")
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    class FakeClient:
        def create_market_order(self, args):
            return {"args": args}

        def post_order(self, order, otype):
            args = order["args"]
            if getattr(args, "side", None) == "SELL":
                # Matched but zero proceeds — must be treated as no-fill, not a
                # $0 sale that books a 100% loss.
                return {"status": "matched", "orderID": "SELL0",
                        "takingAmount": "0", "makingAmount": "0"}
            # BUY: 40 shares for $20.
            return {"status": "matched", "orderID": "BUY1",
                    "takingAmount": "40", "makingAmount": "20"}

        def get_balance_allowance(self, params):
            return {"balance": str(int(40 * 1e6))}

    monkeypatch.setattr(ex, "_get_clob_client", lambda: FakeClient())
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})

    buy = ex.place_order(mode="live", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert buy["status"] == "filled"
    pos_id = buy["position_id"]
    cash_after_buy = state.get_portfolio("live")["cash"]

    closed = ex.close_position(mode="live", position_id=pos_id)
    # The sell yielded nothing — surface no_liquidity/error, never "closed".
    assert closed["status"] in ("no_liquidity", "error")
    assert closed["status"] != "closed"

    # The position must remain OPEN; no phantom $0 settlement / total loss.
    p = state.get_portfolio("live")
    assert p["open_positions"] == 1
    assert state.get_positions("live", status="open")[0]["id"] == pos_id
    assert state.get_positions("live", status="closed") == []
    # Realized PnL untouched (no -$20 phantom loss booked), cash unchanged.
    assert round(p["realized_pnl"], 2) == 0.0
    assert p["cash"] == cash_after_buy
