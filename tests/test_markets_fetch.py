"""Coverage for polymarket_mcp.markets.fetch_market (single-market lookup).

Tests monkeypatch ``markets.httpx.get`` to return a single-market Gamma
payload and assert the direct-by-condition-id lookup parses it correctly and
falls back to None on error/empty results.
"""
import json

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


def _make_resp(payload):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    return FakeResp()


def test_fetch_market_parses_single_gamma_payload(monkeypatch):
    monkeypatch.setattr(markets.httpx, "get", lambda *a, **k: _make_resp([GAMMA_ROW]))

    m = markets.fetch_market("0xabc")

    assert m is not None
    assert isinstance(m, markets.Market)
    assert m.condition_id == "0xabc"
    assert m.question == "Will it rain in NYC tomorrow?"
    assert m.yes_price == 0.40
    assert m.no_price == 0.60
    assert m.category == "weather"
    assert m.volume == 12000.0
    assert markets.get_token_id(m, "YES") == "tok_yes"
    assert markets.get_token_id(m, "NO") == "tok_no"


def test_fetch_market_passes_condition_ids_filter(monkeypatch):
    """The lookup must hit Gamma /markets with a condition_ids filter."""
    captured = {}

    def fake_get(url, *args, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return _make_resp([GAMMA_ROW])

    monkeypatch.setattr(markets.httpx, "get", fake_get)

    markets.fetch_market("0xabc")

    assert captured["url"] == f"{markets.config.GAMMA_API}/markets"
    assert captured["params"]["condition_ids"] == "0xabc"


def test_fetch_market_to_dict_is_json_serializable(monkeypatch):
    monkeypatch.setattr(markets.httpx, "get", lambda *a, **k: _make_resp([GAMMA_ROW]))

    d = markets.fetch_market("0xabc").to_dict()
    assert json.loads(json.dumps(d)) == d  # must round-trip cleanly
    assert d["market_id"] == "0xabc"
    assert d["yes_price"] == 0.40
    assert d["tokens"][0]["token_id"] == "tok_yes"


def test_fetch_market_handles_dict_envelope(monkeypatch):
    """Gamma may wrap rows in a {"data": [...]} envelope."""
    monkeypatch.setattr(
        markets.httpx, "get", lambda *a, **k: _make_resp({"data": [GAMMA_ROW]})
    )

    m = markets.fetch_market("0xabc")
    assert m is not None
    assert m.condition_id == "0xabc"


def test_fetch_market_returns_market_even_for_zero_volume(monkeypatch):
    """Direct lookup must NOT apply fetch_active_markets' liquidity pruning."""
    zero_vol = {**GAMMA_ROW, "volume": "0"}
    monkeypatch.setattr(markets.httpx, "get", lambda *a, **k: _make_resp([zero_vol]))

    m = markets.fetch_market("0xabc")
    assert m is not None
    assert m.volume == 0.0


def test_fetch_market_error_returns_none(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(markets.httpx, "get", boom)

    assert markets.fetch_market("0xabc") is None


def test_fetch_market_empty_list_returns_none(monkeypatch):
    monkeypatch.setattr(markets.httpx, "get", lambda *a, **k: _make_resp([]))
    assert markets.fetch_market("0xabc") is None


def test_fetch_market_empty_condition_id_returns_none():
    assert markets.fetch_market("") is None


def test_fetch_market_non_binary_returns_multi_outcome(monkeypatch):
    """Direct lookup of a non-binary market now returns it as multi-outcome."""
    non_binary = {
        "question": "Who will be the next president?",
        "conditionId": "0xmulti",
        "outcomePrices": "[\"0.40\", \"0.60\"]",
        "outcomes": "[\"Trump\", \"Biden\"]",
        "clobTokenIds": "[\"tok_a\", \"tok_b\"]",
        "volume": "12000",
        "endDate": "2026-06-20",
        "active": True,
        "tags": ["politics"],
    }
    monkeypatch.setattr(markets.httpx, "get", lambda *a, **k: _make_resp([non_binary]))
    m = markets.fetch_market("0xmulti")
    assert m is not None
    assert m.is_binary is False
    assert m.condition_id == "0xmulti"
    assert {t["outcome"] for t in m.tokens} == {"Trump", "Biden"}
