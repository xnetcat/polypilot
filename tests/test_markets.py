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
    assert "hours_to_close" in d  # time-to-resolution surfaced for selection


# --- hours_to_close / time-to-resolution ----------------------------------

def _market(end_date: str) -> markets.Market:
    return markets.Market(
        condition_id="c", question="q", category="other", yes_price=0.5,
        no_price=0.5, volume=1.0, end_date=end_date, active=True,
        tokens=[{"token_id": "t", "outcome": "Yes", "price": 0.5}])


def test_hours_until_parses_z_suffix_and_is_future():
    import datetime
    soon = (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    h = _market(soon).hours_to_close
    assert h is not None
    assert 9 < h < 11  # ~10 hours out


def test_hours_until_negative_when_past():
    import datetime
    past = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _market(past).hours_to_close < 0


def test_hours_until_none_when_unparseable():
    assert _market("").hours_to_close is None
    assert _market("not-a-date").hours_to_close is None


# --- order/fee economics from Gamma ---------------------------------------

def test_parse_market_economics_from_gamma():
    m = {"orderMinSize": 5, "orderPriceMinTickSize": "0.01", "feesEnabled": True,
         "feeSchedule": {"rate": 0.03, "exponent": 1, "takerOnly": True}}
    econ = markets._parse_market_economics(m)
    assert econ["min_order_size"] == 5.0
    assert econ["tick_size"] == 0.01
    assert econ["fees_enabled"] is True
    assert econ["fee_rate"] == 0.03
    assert econ["fee_exponent"] == 1.0


def test_parse_market_economics_fees_disabled_forces_zero_rate():
    # feesEnabled False -> fee_rate 0 regardless of any schedule present.
    econ = markets._parse_market_economics(
        {"orderMinSize": 5, "feesEnabled": False, "feeSchedule": {"rate": 0.05}})
    assert econ["fees_enabled"] is False
    assert econ["fee_rate"] == 0.0


def test_parse_market_economics_defaults_when_missing():
    assert markets._parse_market_economics({}) == {
        "min_order_size": 5.0, "tick_size": 0.01,
        "fee_rate": 0.0, "fee_exponent": 1.0, "fees_enabled": False}


def test_economics_flow_into_market_dict(monkeypatch):
    row = dict(GAMMA_ROW, orderMinSize=5, orderPriceMinTickSize="0.01",
               feesEnabled=True, feeSchedule={"rate": 0.04, "exponent": 1})

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return [row]
    monkeypatch.setattr(markets.httpx, "get", lambda *a, **k: FakeResp())
    d = markets.fetch_active_markets(limit=1)[0].to_dict()
    assert d["min_order_size"] == 5.0
    assert d["tick_size"] == 0.01
    assert d["fee_rate"] == 0.04
    assert d["fees_enabled"] is True


def test_malformed_outcome_prices_does_not_crash(monkeypatch):
    # outcomePrices is truthy but invalid JSON — must not raise NameError on
    # the unbound `prices` variable; the row should parse with default 0.5/0.5
    # prices (and may be skipped only by the legitimate price/volume filters).
    bad_row = {**GAMMA_ROW, "outcomePrices": "not-json", "volume": "5000"}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return [bad_row]
    monkeypatch.setattr(markets.httpx, "get", lambda *a, **k: FakeResp())
    result = markets.fetch_active_markets(limit=10)  # must not raise
    assert isinstance(result, list)
