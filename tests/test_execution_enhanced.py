# tests/test_execution_enhanced.py
"""Coverage for the enhanced execution layer:

  - slippage-aware paper fills that walk the asks ladder (weighted avg price)
  - fallback to best_ask when no detailed `asks` list (identical to old behavior)
  - partial fill on a shallow book
  - closed-market guard rejects (paper + live)
  - invalid side rejected
  - live limit order path (GTC, mocked client)
  - get_live_balance / get_live_positions / sync_live_positions / preflight_live

All network/CLOB access is mocked. The closed-market guard (`_market_is_closed`)
issues an httpx.get; every test that reaches a fill stubs it to "open" so the
guard never blocks a fill it shouldn't.
"""
import importlib

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "100")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_DAILY_USD", "30")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_TOTAL_USD", "40")
    import polymarket_mcp.config as cfg
    import polymarket_mcp.db as db
    import polymarket_mcp.state as state
    import polymarket_mcp.order_book as ob
    import polymarket_mcp.risk as risk
    import polymarket_mcp.execution as ex
    for m in (cfg, db, state, ob, risk, ex):
        importlib.reload(m)
    # Default: every market is OPEN, so the closed-guard never blocks a fill
    # unless a test overrides it. Avoids real network calls too.
    monkeypatch.setattr(ex, "_market_is_closed", lambda market_id: False)
    return state, ex


def _book(asks=None, best_ask=None, best_bid=0.48):
    """Build a fetch_order_book-style dict. `asks` is a list of
    {price,size} levels; best_ask defaults to the top of the ladder."""
    book = {"best_bid": best_bid}
    if asks is not None:
        book["asks"] = asks
        book["best_ask"] = asks[0]["price"] if asks else None
    if best_ask is not None:
        book["best_ask"] = best_ask
    return book


# --------------------------------------------------------------------------- #
# 1. Slippage-aware multi-level fill
# --------------------------------------------------------------------------- #
def test_slippage_multi_level_weighted_fill(env, monkeypatch):
    state, ex = env
    # Three levels. We'll spend $30:
    #   L1: 40 sh @ 0.50 = $20  (consume fully -> 40 sh, $20)
    #   L2: remaining $10 @ 0.55 -> 10/0.55 = 18.1818.. sh
    # total shares = 58.18.., spent = $30
    asks = [
        {"price": 0.50, "size": 40},
        {"price": 0.55, "size": 100},
        {"price": 0.60, "size": 100},
    ]
    monkeypatch.setattr(ex.order_book, "fetch_order_book", lambda tid: _book(asks=asks))
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=30.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "filled"
    assert "partial" not in res  # full size was filled
    assert res["size_usd"] == 30.0

    l2_shares = 10.0 / 0.55
    expected_shares = round(40.0 + l2_shares, 2)
    expected_avg = round(30.0 / (40.0 + l2_shares), 4)
    assert res["shares"] == expected_shares
    assert res["fill_price"] == expected_avg
    # Weighted avg must sit strictly between the two levels touched.
    assert 0.50 < res["fill_price"] < 0.55
    # Cash debited by the spend.
    assert state.get_portfolio("paper")["cash"] == 70.0


def test_slippage_single_level_exact(env, monkeypatch):
    # If the top level can absorb the whole order, avg price == that level.
    state, ex = env
    asks = [{"price": 0.40, "size": 1000}, {"price": 0.42, "size": 1000}]
    monkeypatch.setattr(ex.order_book, "fetch_order_book", lambda tid: _book(asks=asks))
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "filled"
    assert res["fill_price"] == 0.40
    assert res["shares"] == 50.0  # 20 / 0.40
    assert "partial" not in res


# --------------------------------------------------------------------------- #
# 2. Fallback to best_ask when no `asks` ladder (identical to old behavior)
# --------------------------------------------------------------------------- #
def test_fallback_to_best_ask_no_asks_list(env, monkeypatch):
    state, ex = env
    # Mock with ONLY best_ask/best_bid (the shape existing tests use).
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    # Identical to the legacy behavior asserted in test_execution.py.
    assert res["status"] == "filled"
    assert res["fill_price"] == 0.50
    assert res["shares"] == 40.0
    assert res["size_usd"] == 20.0
    assert "partial" not in res
    assert state.get_portfolio("paper")["cash"] == 80.0


def test_fallback_empty_asks_list_uses_best_ask(env, monkeypatch):
    # An explicit empty asks list is still "no ladder" -> fall back to best_ask.
    state, ex = env
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"asks": [], "best_ask": 0.50, "best_bid": 0.48})
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "filled"
    assert res["fill_price"] == 0.50
    assert res["shares"] == 40.0


def test_no_liquidity_when_neither_asks_nor_best_ask(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": None, "best_bid": 0.48})
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "rejected"
    assert "liquidity" in res["reason"].lower()
    assert state.get_positions("paper") == []


# --------------------------------------------------------------------------- #
# 3. Partial fill on a shallow book
# --------------------------------------------------------------------------- #
def test_partial_fill_on_shallow_book(env, monkeypatch):
    state, ex = env
    # Only $12 of depth available, but we ask for $20.
    #   L1: 10 sh @ 0.50 = $5
    #   L2: 10 sh @ 0.70 = $7   -> total $12, 20 shares
    asks = [{"price": 0.50, "size": 10}, {"price": 0.70, "size": 10}]
    monkeypatch.setattr(ex.order_book, "fetch_order_book", lambda tid: _book(asks=asks))
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "filled"
    assert res["partial"] is True
    # Filled portion only: 20 shares for $12.
    assert res["shares"] == 20.0
    assert res["size_usd"] == 12.0
    assert res["fill_price"] == round(12.0 / 20.0, 4)  # 0.60
    # Position opened for the FILLED portion; cash debited by $12 only.
    assert state.get_portfolio("paper")["cash"] == round(100.0 - 12.0, 2)
    open_pos = state.get_positions("paper", status="open")
    assert len(open_pos) == 1
    assert open_pos[0]["shares"] == 20.0


def test_paper_cash_capped_partial_deep_book(env, monkeypatch):
    # $100 requested, only $90 cash, deep book -> fills $90 (cash-capped partial),
    # cash -> ~0, conservation holds. The book itself could absorb the full $100,
    # so the cap is purely the cash.
    state, ex = env
    # Spend $10 of the starting $100 first so cash = $90.
    asks_seed = [{"price": 0.50, "size": 10000}]
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: _book(asks=asks_seed))
    seed = ex.place_order(mode="paper", market_id="seed", token_id="seed",
        question="Q", category="weather", side="YES", size_usd=10.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert seed["status"] == "filled"
    assert state.get_portfolio("paper")["cash"] == 90.0

    # Now request $100 against a book deep enough for the full amount.
    deep = [{"price": 0.50, "size": 10000}]
    monkeypatch.setattr(ex.order_book, "fetch_order_book", lambda tid: _book(asks=deep))
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=100.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "filled"
    assert res["partial"] is True  # cash-capped
    # Spend exactly the available cash, never more.
    assert res["size_usd"] == 90.0
    assert res["shares"] == round(90.0 / 0.50, 2)  # 180 shares
    # Cash conservation: debit equals exactly the available cash -> ~0.
    assert state.get_portfolio("paper")["cash"] == 0.0


def test_paper_cash_capped_but_depth_capped_first(env, monkeypatch):
    # $100 requested, $90 cash, but only ~$80 of book depth -> fills ~$80
    # (depth-capped partial), strictly within cash.
    state, ex = env
    asks_seed = [{"price": 0.50, "size": 10000}]
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: _book(asks=asks_seed))
    ex.place_order(mode="paper", market_id="seed", token_id="seed",
        question="Q", category="weather", side="YES", size_usd=10.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert state.get_portfolio("paper")["cash"] == 90.0

    # Book depth: 160 shares @ 0.50 = $80 total — shallower than both the
    # requested $100 AND the $90 cash.
    shallow = [{"price": 0.50, "size": 160}]
    monkeypatch.setattr(ex.order_book, "fetch_order_book", lambda tid: _book(asks=shallow))
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=100.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "filled"
    assert res["partial"] is True  # depth-capped
    assert res["size_usd"] == 80.0
    assert res["shares"] == 160.0
    # Spend stayed within available cash (and didn't reach the cash cap).
    assert state.get_portfolio("paper")["cash"] == 10.0
    assert res["size_usd"] < 90.0


def test_paper_cash_capped_fallback_best_ask(env, monkeypatch):
    # Cash cap also applies on the best_ask fallback path (no asks ladder).
    state, ex = env
    asks_seed = [{"price": 0.50, "size": 10000}]
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: _book(asks=asks_seed))
    ex.place_order(mode="paper", market_id="seed", token_id="seed",
        question="Q", category="weather", side="YES", size_usd=10.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert state.get_portfolio("paper")["cash"] == 90.0

    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=100.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "filled"
    assert res["partial"] is True
    assert res["size_usd"] == 90.0
    assert res["shares"] == round(90.0 / 0.50, 2)  # 180 shares
    assert state.get_portfolio("paper")["cash"] == 0.0


def test_paper_rejects_when_no_cash(env, monkeypatch):
    # Paper rejects only when cash <= 0 (here we drain it to exactly 0 first).
    state, ex = env
    deep = [{"price": 0.50, "size": 10000}]
    monkeypatch.setattr(ex.order_book, "fetch_order_book", lambda tid: _book(asks=deep))
    drain = ex.place_order(mode="paper", market_id="seed", token_id="seed",
        question="Q", category="weather", side="YES", size_usd=100.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert drain["status"] == "filled"
    assert state.get_portfolio("paper")["cash"] == 0.0
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "rejected"
    assert "cash" in res["reason"].lower()


def test_live_rejects_when_over_cash(env, monkeypatch):
    # LIVE still hard-rejects size_usd > tracked cash (no live order over cash).
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    res = ex.place_order(mode="live", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=200.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "rejected"
    assert "cash" in res["reason"].lower()
    assert state.get_positions("live") == []


def test_slippage_limit_price_caps_walk(env, monkeypatch):
    # The walk stops at the limit price; deeper, pricier levels are skipped.
    state, ex = env
    asks = [{"price": 0.50, "size": 10}, {"price": 0.60, "size": 100}]
    monkeypatch.setattr(ex.order_book, "fetch_order_book", lambda tid: _book(asks=asks))
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t", limit_price=0.55)
    # Only the 0.50 level qualifies: 10 sh for $5; the 0.60 level is above limit.
    assert res["status"] == "filled"
    assert res["partial"] is True
    assert res["shares"] == 10.0
    assert res["size_usd"] == 5.0
    assert res["fill_price"] == 0.50


def test_slippage_limit_price_rejects_when_top_above_limit(env, monkeypatch):
    state, ex = env
    asks = [{"price": 0.60, "size": 100}]
    monkeypatch.setattr(ex.order_book, "fetch_order_book", lambda tid: _book(asks=asks))
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t", limit_price=0.50)
    assert res["status"] == "rejected"
    assert "limit" in res["reason"].lower()
    assert state.get_positions("paper") == []


# --------------------------------------------------------------------------- #
# 4. Closed-market guard
# --------------------------------------------------------------------------- #
def test_closed_market_guard_is_live_only_not_called_in_paper(env, monkeypatch):
    """The closed-market guard does a live status fetch, so it is gated to LIVE
    mode. In PAPER mode (simulation) it must NEVER be consulted — proving the
    suite stays hermetic (no network) for the common paper path. We assert the
    guard is not even invoked, and that a paper order still fills normally."""
    state, ex = env

    called = {"n": 0}

    def _boom_if_called(market_id):
        called["n"] += 1
        raise AssertionError("closed-market guard must not run in paper mode")

    monkeypatch.setattr(ex, "_market_is_closed", _boom_if_called)
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    # Paper fills as usual; the guard was never consulted.
    assert res["status"] == "filled"
    assert called["n"] == 0
    assert state.get_portfolio("paper")["cash"] == 80.0


def test_closed_market_guard_rejects_live(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    monkeypatch.setattr(ex, "_market_is_closed", lambda market_id: True)
    res = ex.place_order(mode="live", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "rejected"
    assert "closed" in res["reason"].lower() or "resolved" in res["reason"].lower()
    assert state.get_positions("live") == []


def test_market_is_closed_helper(env, monkeypatch):
    # Exercise the REAL helper logic with a mocked httpx.get. The env fixture
    # stubs _market_is_closed to False; reload the module here to get the
    # genuine implementation back without the stub.
    _, ex = env
    importlib.reload(ex)

    class _Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
        def json(self):
            return self._payload

    # closed=True -> True
    monkeypatch.setattr(ex.httpx, "get",
        lambda *a, **k: _Resp(200, {"closed": True}))
    assert ex._market_is_closed("mkt") is True
    # closed=False -> False
    monkeypatch.setattr(ex.httpx, "get",
        lambda *a, **k: _Resp(200, {"closed": False}))
    assert ex._market_is_closed("mkt") is False
    # non-200 -> False (best-effort, don't block)
    monkeypatch.setattr(ex.httpx, "get",
        lambda *a, **k: _Resp(503, {}))
    assert ex._market_is_closed("mkt") is False
    # raises -> False
    def _boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(ex.httpx, "get", _boom)
    assert ex._market_is_closed("mkt") is False


# --------------------------------------------------------------------------- #
# 5. Side validation: blank rejected; multi-outcome labels accepted
# --------------------------------------------------------------------------- #
def test_blank_side_rejected(env, monkeypatch):
    # Only a blank/whitespace (or non-string) side is rejected now. Multi-outcome
    # markets buy a specific OUTCOME token whose label IS the side, so arbitrary
    # non-YES/NO labels must be accepted (see test below).
    state, ex = env
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="sports", side="   ", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "rejected"
    assert "side" in res["reason"].lower()
    assert state.get_positions("paper") == []
    assert state.get_portfolio("paper")["cash"] == 100.0


def test_multi_outcome_label_side_accepted(env, monkeypatch):
    # A multi-outcome label (e.g. "Lakers") is a valid side: it buys that
    # outcome's token. The label's original case is preserved (not upper-cased
    # like YES/NO).
    state, ex = env
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok_lakers",
        question="NBA Champion?", category="sports", side="Lakers",
        size_usd=20.0, fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "filled"
    assert res["side"] == "Lakers"  # case preserved for non-binary labels
    assert res["shares"] == 40.0    # 20 / 0.50
    open_pos = state.get_positions("paper", status="open")
    assert len(open_pos) == 1
    assert open_pos[0]["side"] == "Lakers"
    assert state.get_portfolio("paper")["cash"] == 80.0


def test_side_case_insensitive_accepted(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.order_book, "fetch_order_book",
        lambda tid: {"best_ask": 0.50, "best_bid": 0.48})
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="yes", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t")
    assert res["status"] == "filled"
    assert res["side"] == "YES"  # normalized to upper


# --------------------------------------------------------------------------- #
# 6. Live limit order path (GTC, mocked client)
# --------------------------------------------------------------------------- #
def test_live_limit_order_immediate_fill(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    captured = {}

    class FakeOrderArgs:
        def __init__(self, token_id, price, size, side):
            self.token_id, self.price, self.size, self.side = token_id, price, size, side

    class FakeOrderType:
        GTC = "GTC"
        FAK = "FAK"

    fake_clob_types = type("M", (), {"OrderArgs": FakeOrderArgs,
                                     "OrderType": FakeOrderType})

    class FakeClient:
        def create_order(self, args):
            captured["args"] = args
            return {"signed": True}
        def post_order(self, order, otype):
            captured["otype"] = otype
            # Filled immediately: 40 shares for $20.
            return {"status": "matched", "orderID": "LIMIT1",
                    "takingAmount": "40", "makingAmount": "20"}

    monkeypatch.setattr(ex, "_get_clob_client", lambda: FakeClient())
    # Inject our fake py_clob_client_v2.clob_types module for the local import.
    import sys
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", type("P", (), {}))
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", fake_clob_types)

    res = ex.place_order(mode="live", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t",
        order_type="limit", limit_price=0.50)
    assert res["status"] == "filled"
    assert res["order_id"] == "LIMIT1"
    assert res["order_type"] == "limit"
    assert res["resting"] is False
    assert res["shares"] == 40.0
    assert res["fill_price"] == 0.50
    # GTC was used, BUY side, price/size set from the limit.
    assert captured["otype"] == "GTC"
    assert captured["args"].side == "BUY"
    assert captured["args"].price == 0.50
    assert captured["args"].size == 40.0  # 20 / 0.50


def test_live_limit_order_resting(env, monkeypatch):
    # A GTC order that rests (status 'live', no immediate fill) still opens a
    # position from the intended size at the limit price.
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    class FakeOrderArgs:
        def __init__(self, token_id, price, size, side):
            self.token_id, self.price, self.size, self.side = token_id, price, size, side

    class FakeOrderType:
        GTC = "GTC"

    fake_clob_types = type("M", (), {"OrderArgs": FakeOrderArgs,
                                     "OrderType": FakeOrderType})

    class FakeClient:
        def create_order(self, args):
            return {"signed": True}
        def post_order(self, order, otype):
            return {"status": "live", "orderID": "REST1",
                    "takingAmount": "0", "makingAmount": "0"}

    monkeypatch.setattr(ex, "_get_clob_client", lambda: FakeClient())
    import sys
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", type("P", (), {}))
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", fake_clob_types)

    res = ex.place_order(mode="live", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t",
        order_type="limit", limit_price=0.40)
    assert res["status"] == "filled"
    assert res["resting"] is True
    assert res["shares"] == 50.0  # 20 / 0.40
    assert res["fill_price"] == 0.40
    # Position opened from the resting size at the limit price.
    pos = state.get_positions("live", status="open")
    assert len(pos) == 1
    assert pos[0]["shares"] == 50.0
    assert pos[0]["size_usd"] == 20.0  # 50 shares * 0.40


def test_live_limit_requires_limit_price(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    res = ex.place_order(mode="live", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t",
        order_type="limit", limit_price=None)
    assert res["status"] == "rejected"
    assert "limit_price" in res["reason"]


def test_live_limit_missing_api_returns_error(env, monkeypatch):
    # If the installed client lacks the limit API (import fails), return a clear
    # error rather than raising.
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "py_clob_client_v2.clob_types":
            raise ImportError("no OrderArgs in this build")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    res = ex.place_order(mode="live", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t",
        order_type="limit", limit_price=0.50)
    assert res["status"] == "error"
    assert "limit" in res["reason"].lower()
    assert state.get_positions("live") == []


def test_invalid_order_type_rejected(env, monkeypatch):
    state, ex = env
    res = ex.place_order(mode="paper", market_id="m1", token_id="tok",
        question="Q", category="weather", side="YES", size_usd=20.0,
        fair_prob=0.6, confidence=0.7, thesis="t", order_type="stop")
    assert res["status"] == "rejected"
    assert "order_type" in res["reason"]


# --------------------------------------------------------------------------- #
# 7. get_live_balance
# --------------------------------------------------------------------------- #
def test_get_live_balance_no_key(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "")
    res = ex.get_live_balance()
    assert "error" in res
    assert res["balance_usd"] is None


def test_get_live_balance_with_client(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    class FakeAssetType:
        COLLATERAL = "COLLATERAL"

    class FakeBalanceParams:
        def __init__(self, asset_type=None, token_id=None):
            self.asset_type = asset_type

    fake_clob_types = type("M", (), {"AssetType": FakeAssetType,
                                     "BalanceAllowanceParams": FakeBalanceParams})

    class FakeClient:
        def get_balance_allowance(self, params):
            # 123.45 USDC in 1e6 units.
            return {"balance": str(int(123.45 * 1e6))}

    monkeypatch.setattr(ex, "_get_clob_client", lambda: FakeClient())
    import sys
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", type("P", (), {}))
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", fake_clob_types)

    res = ex.get_live_balance()
    assert res["balance_usd"] == 123.45
    assert "error" not in res


def test_get_live_balance_client_error(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    fake_clob_types = type("M", (), {"AssetType": type("A", (), {"COLLATERAL": "C"}),
                                     "BalanceAllowanceParams": lambda **k: None})

    def _boom():
        raise RuntimeError("api down")

    class FakeClient:
        def get_balance_allowance(self, params):
            raise RuntimeError("api down")

    monkeypatch.setattr(ex, "_get_clob_client", lambda: FakeClient())
    import sys
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", type("P", (), {}))
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", fake_clob_types)

    res = ex.get_live_balance()
    assert "error" in res
    assert res["balance_usd"] is None


# --------------------------------------------------------------------------- #
# 8. get_live_positions
# --------------------------------------------------------------------------- #
def test_get_live_positions_no_key(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "")
    assert ex.get_live_positions() == []


def test_get_live_positions_aggregates_net(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    trades = [
        # market A YES: bought 100 @ 0.40, sold 30 -> net 70
        {"market": "A", "outcome": "Yes", "side": "BUY", "size": "60",
         "price": "0.40", "asset_id": "assetA"},
        {"market": "A", "outcome": "Yes", "side": "BUY", "size": "40",
         "price": "0.40", "asset_id": "assetA"},
        {"market": "A", "outcome": "Yes", "side": "SELL", "size": "30",
         "price": "0.55", "asset_id": "assetA"},
        # market B NO: bought 10, sold 10 -> net 0 (excluded)
        {"market": "B", "outcome": "No", "side": "BUY", "size": "10",
         "price": "0.20", "asset_id": "assetB"},
        {"market": "B", "outcome": "No", "side": "SELL", "size": "10",
         "price": "0.30", "asset_id": "assetB"},
    ]

    class FakeClient:
        def get_trades(self):
            return trades

    monkeypatch.setattr(ex, "_get_clob_client", lambda: FakeClient())
    res = ex.get_live_positions()
    assert len(res) == 1
    pos = res[0]
    assert pos["market_id"] == "A"
    assert pos["shares"] == 70.0
    assert pos["avg_price"] == 0.40
    assert pos["asset_id"] == "assetA"


def test_get_live_positions_client_error_returns_empty(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    class FakeClient:
        def get_trades(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(ex, "_get_clob_client", lambda: FakeClient())
    assert ex.get_live_positions() == []


# --------------------------------------------------------------------------- #
# 9. sync_live_positions
# --------------------------------------------------------------------------- #
def test_sync_reports_discrepancies_without_mutating(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    # Local open positions: markets "L1" (local-only) and "BOTH".
    state.open_position("live", market_id="L1", token_id="tL1", question="q",
        category="c", side="YES", entry_price=0.5, size_usd=10, shares=20,
        fair_prob=0.5, confidence=0.5, thesis="t")
    state.open_position("live", market_id="BOTH", token_id="tB", question="q",
        category="c", side="YES", entry_price=0.5, size_usd=10, shares=20,
        fair_prob=0.5, confidence=0.5, thesis="t")

    # On-chain: markets "BOTH" (matched) and "C1" (chain-only). The fetch
    # SUCCEEDS (error=None), so a real diff is reported.
    monkeypatch.setattr(ex, "_fetch_live_positions", lambda: ([
        {"market_id": "BOTH", "outcome": "Yes", "shares": 20.0, "avg_price": 0.5},
        {"market_id": "C1", "outcome": "Yes", "shares": 5.0, "avg_price": 0.3},
    ], None))

    diff = ex.sync_live_positions(mode="live")
    assert diff["in_sync"] is False
    assert diff["on_chain_count"] == 2
    assert diff["local_count"] == 2
    assert [p["market_id"] for p in diff["on_chain_only"]] == ["C1"]
    assert [p["market_id"] for p in diff["local_only"]] == ["L1"]
    assert diff["matched"] == ["BOTH"]

    # State must NOT have been mutated by a read-only sync.
    open_after = state.get_positions("live", status="open")
    assert {p["market_id"] for p in open_after} == {"L1", "BOTH"}
    assert state.get_portfolio("live")["open_positions"] == 2


def test_sync_in_sync_when_matched(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    state.open_position("live", market_id="X", token_id="tX", question="q",
        category="c", side="YES", entry_price=0.5, size_usd=10, shares=20,
        fair_prob=0.5, confidence=0.5, thesis="t")
    monkeypatch.setattr(ex, "_fetch_live_positions", lambda: ([
        {"market_id": "X", "outcome": "Yes", "shares": 20.0, "avg_price": 0.5},
    ], None))
    diff = ex.sync_live_positions(mode="live")
    assert diff["in_sync"] is True
    assert diff["on_chain_only"] == []
    assert diff["local_only"] == []
    assert diff["matched"] == ["X"]


def test_sync_no_key_returns_error_not_in_sync(env, monkeypatch):
    # Without a private key we cannot read the chain, so sync must NOT claim
    # in_sync. It returns an explicit error form with in_sync False and reports
    # the local count.
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "")
    state.open_position("live", market_id="L1", token_id="tL1", question="q",
        category="c", side="YES", entry_price=0.5, size_usd=10, shares=20,
        fair_prob=0.5, confidence=0.5, thesis="t")
    diff = ex.sync_live_positions(mode="live")
    assert diff["in_sync"] is False
    assert diff["error"] == "POLYMARKET_PRIVATE_KEY not set"
    assert diff["on_chain_count"] == 0
    assert diff["local_count"] == 1
    assert diff["on_chain_only"] == []
    assert diff["local_only"] == []
    assert diff["matched"] == []


def test_sync_fetch_failure_not_in_sync(env, monkeypatch):
    # A failed on-chain fetch must be surfaced as fetch_error with in_sync False,
    # NOT silently treated as an empty chain that "matches" an empty local book.
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    monkeypatch.setattr(ex, "_fetch_live_positions",
        lambda: ([], "RuntimeError: chain unreachable"))
    diff = ex.sync_live_positions(mode="live")
    assert diff["in_sync"] is False
    assert "fetch_error" in diff
    assert diff["on_chain_count"] == 0
    assert diff["local_count"] == 0


# --------------------------------------------------------------------------- #
# 10. preflight_live
# --------------------------------------------------------------------------- #
def test_preflight_all_green(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    monkeypatch.setattr(ex, "_get_clob_client", lambda: object())
    monkeypatch.setattr(ex, "get_live_balance", lambda: {"balance_usd": 150.0})
    res = ex.preflight_live()
    assert res["ok"] is True
    c = res["checks"]
    assert c["private_key_set"] is True
    assert c["clob_creds_derivable"] is True
    assert c["balance_positive"] is True
    assert c["balance_usd"] == 150.0
    assert c["kill_switch_configured"] is True


def test_preflight_no_key(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "")
    res = ex.preflight_live()
    assert res["ok"] is False
    c = res["checks"]
    assert c["private_key_set"] is False
    assert c["clob_creds_derivable"] is False
    assert c["balance_positive"] is False
    # Balance check skipped without a key.
    assert c["balance_usd"] is None


def test_preflight_zero_balance_fails(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    monkeypatch.setattr(ex, "_get_clob_client", lambda: object())
    monkeypatch.setattr(ex, "get_live_balance", lambda: {"balance_usd": 0.0})
    res = ex.preflight_live()
    assert res["ok"] is False
    assert res["checks"]["balance_positive"] is False


def test_preflight_creds_derivation_error(env, monkeypatch):
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    def _boom():
        raise RuntimeError("bad creds")

    monkeypatch.setattr(ex, "_get_clob_client", _boom)
    monkeypatch.setattr(ex, "get_live_balance", lambda: {"balance_usd": 100.0})
    res = ex.preflight_live()
    assert res["ok"] is False
    assert res["checks"]["clob_creds_derivable"] is False
    assert "bad creds" in res["checks"]["clob_creds_detail"]


def test_preflight_never_raises(env, monkeypatch):
    # Even with everything throwing, preflight returns a dict.
    _, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    def _boom(*a, **k):
        raise RuntimeError("explode")

    monkeypatch.setattr(ex, "_get_clob_client", _boom)
    monkeypatch.setattr(ex, "get_live_balance", _boom)
    res = ex.preflight_live()
    assert isinstance(res, dict)
    assert res["ok"] is False
