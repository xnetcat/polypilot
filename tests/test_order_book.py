import polymarket_mcp.order_book as ob


def test_order_book_metrics():
    book = ob.OrderBook(
        bids=[ob.OrderBookLevel(0.40, 100), ob.OrderBookLevel(0.39, 50)],
        asks=[ob.OrderBookLevel(0.42, 30)])
    assert book.bid_volume == 150
    assert book.ask_volume == 30
    assert round(book.imbalance, 3) == round((150 - 30) / 180, 3)
    assert round(book.spread, 2) == 0.02
    assert round(book.midpoint, 3) == 0.41


def test_fetch_order_book_parses_clob(monkeypatch):
    payload = {"bids": [{"price": "0.40", "size": "100"}],
               "asks": [{"price": "0.42", "size": "30"}]}
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return payload
    monkeypatch.setattr(ob.httpx, "get", lambda *a, **k: FakeResp())
    result = ob.fetch_order_book("tok_yes")
    assert result["best_bid"] == 0.40
    assert result["best_ask"] == 0.42
    assert result["bids"][0] == {"price": 0.40, "size": 100.0}
