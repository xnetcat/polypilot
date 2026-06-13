import polymarket_mcp.markets as markets


GAMMA_ROW = {
    "question": "Will it rain in NYC tomorrow?",
    "conditionId": "0xabc",
    "outcomePrices": "[\"0.40\", \"0.60\"]",
    "outcomes": "[\"Yes\", \"No\"]",
    "clobTokenIds": "[\"tok_yes\", \"tok_no\"]",
    "volume": "12000",
    "endDate": "2026-06-20",
    "active": True,
    "tags": ["weather"],
}


def test_fetch_active_markets_parses_gamma(monkeypatch):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return [GAMMA_ROW]
    monkeypatch.setattr(markets.httpx, "get", lambda *a, **k: FakeResp())
    result = markets.fetch_active_markets(limit=10)
    assert len(result) == 1
    m = result[0]
    assert m.question == "Will it rain in NYC tomorrow?"
    assert m.yes_price == 0.40
    assert m.category == "weather"
    assert markets.get_token_id(m, "YES") == "tok_yes"


def test_to_dict_is_json_serializable(monkeypatch):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return [GAMMA_ROW]
    monkeypatch.setattr(markets.httpx, "get", lambda *a, **k: FakeResp())
    import json
    d = markets.fetch_active_markets(limit=1)[0].to_dict()
    json.dumps(d)  # must not raise
    assert d["yes_price"] == 0.40
