"""Extra breadth coverage for polymarket_mcp.order_book.

Tests assert CURRENT correct behavior and monkeypatch ``order_book.httpx.get``.
"""
import polymarket_mcp.order_book as ob


def _make_resp(payload):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    return FakeResp()


# ---------------------------------------------------------------------------
# OrderBook with empty bids/asks
# ---------------------------------------------------------------------------

def test_empty_order_book_metrics():
    book = ob.OrderBook(bids=[], asks=[])
    assert book.spread == 0.0
    assert book.midpoint == 0.0
    assert book.imbalance == 0.0
    assert book.bid_volume == 0
    assert book.ask_volume == 0


# ---------------------------------------------------------------------------
# fetch_order_book error path
# ---------------------------------------------------------------------------

def test_fetch_order_book_error_path(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(ob.httpx, "get", boom)
    result = ob.fetch_order_book("tok_err")
    assert "error" in result
    assert result["token_id"] == "tok_err"


# ---------------------------------------------------------------------------
# fetch_order_book with UNSORTED raw levels (guards the sort)
# ---------------------------------------------------------------------------

def test_fetch_order_book_sorts_unsorted_levels(monkeypatch):
    payload = {
        # bids deliberately out of order -> best bid is the HIGHEST (0.45)
        "bids": [
            {"price": "0.40", "size": "100"},
            {"price": "0.45", "size": "50"},
            {"price": "0.38", "size": "25"},
        ],
        # asks deliberately out of order -> best ask is the LOWEST (0.47)
        "asks": [
            {"price": "0.55", "size": "10"},
            {"price": "0.47", "size": "20"},
            {"price": "0.50", "size": "30"},
        ],
    }
    monkeypatch.setattr(ob.httpx, "get", lambda *a, **k: _make_resp(payload))
    result = ob.fetch_order_book("tok_sort")

    assert result["best_bid"] == 0.45
    assert result["best_ask"] == 0.47
    # spread = best_ask - best_bid = 0.47 - 0.45 = 0.02
    assert result["spread"] == 0.02
    # bid_volume = 175, ask_volume = 60 -> imbalance = (175-60)/235
    assert result["bid_volume"] == 175.0
    assert result["ask_volume"] == 60.0
    assert result["imbalance"] == round((175.0 - 60.0) / 235.0, 4)
    # bids sorted descending, asks ascending
    assert [b["price"] for b in result["bids"]] == [0.45, 0.40, 0.38]
    assert [a["price"] for a in result["asks"]] == [0.47, 0.50, 0.55]


# ---------------------------------------------------------------------------
# fetch_price_history
# ---------------------------------------------------------------------------

def test_fetch_price_history_empty(monkeypatch):
    monkeypatch.setattr(ob.httpx, "get", lambda *a, **k: _make_resp({"history": []}))
    result = ob.fetch_price_history("tok_empty")
    assert result["points"] == 0
    assert result["token_id"] == "tok_empty"


def test_fetch_price_history_error_path(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("timeout")

    monkeypatch.setattr(ob.httpx, "get", boom)
    result = ob.fetch_price_history("tok_err")
    assert "error" in result
    assert result["token_id"] == "tok_err"
