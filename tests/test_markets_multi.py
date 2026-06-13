"""Multi-outcome market discovery coverage for polymarket_mcp.markets.

Polymarket has markets with N>2 candidate tokens (e.g. "Who wins X?"). These
must pass discovery as multi-outcome Markets, while binary YES/NO markets keep
their exact previous shape. Tests monkeypatch ``markets.httpx.get``.
"""
import json

import polymarket_mcp.markets as markets


def _make_resp(payload):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    return FakeResp()


# A 3-outcome ("Who wins?") Gamma payload.
MULTI_ROW = {
    "question": "Who will win the championship?",
    "conditionId": "0xmulti3",
    "outcomePrices": "[\"0.50\", \"0.30\", \"0.20\"]",
    "outcomes": "[\"Alice\", \"Bob\", \"Carol\"]",
    "clobTokenIds": "[\"tok_a\", \"tok_b\", \"tok_c\"]",
    "volume": "25000",
    "endDate": "2026-09-01",
    "active": True,
    "tags": ["sports"],
}

BINARY_ROW = {
    "question": "Will it rain in NYC tomorrow?",
    "conditionId": "0xbin",
    "outcomePrices": "[\"0.40\", \"0.60\"]",
    "outcomes": "[\"Yes\", \"No\"]",
    "clobTokenIds": "[\"tok_yes\", \"tok_no\"]",
    "volume": "12000",
    "endDate": "2026-06-20",
    "active": True,
    "tags": ["weather"],
}


def test_three_outcome_market_parses_via_fetch_active(monkeypatch):
    monkeypatch.setattr(markets.httpx, "get", lambda *a, **k: _make_resp([MULTI_ROW]))

    result = markets.fetch_active_markets(limit=10)
    assert len(result) == 1
    m = result[0]

    assert m.is_binary is False
    assert m.condition_id == "0xmulti3"
    # all three candidate tokens are surfaced
    assert len(m.tokens) == 3

    d = m.to_dict()
    assert d["is_binary"] is False
    assert d["outcomes"] == [
        {"outcome": "Alice", "token_id": "tok_a", "price": 0.50},
        {"outcome": "Bob", "token_id": "tok_b", "price": 0.30},
        {"outcome": "Carol", "token_id": "tok_c", "price": 0.20},
    ]
    # labels, token_ids and prices all present per outcome
    for o in d["outcomes"]:
        assert o["outcome"]
        assert o["token_id"]
        assert isinstance(o["price"], float)

    # JSON-serializable and round-trips cleanly
    assert json.loads(json.dumps(d)) == d


def test_three_outcome_market_parses_via_fetch_market(monkeypatch):
    monkeypatch.setattr(markets.httpx, "get", lambda *a, **k: _make_resp([MULTI_ROW]))

    m = markets.fetch_market("0xmulti3")
    assert m is not None
    assert m.is_binary is False
    assert len(m.tokens) == 3
    json.dumps(m.to_dict())  # must not raise


def test_multi_outcome_extreme_priced_token_not_dropped(monkeypatch):
    """A long-shot/lock token at 0.0/1.0 must NOT drop a multi-outcome market.

    The price-extreme prune is binary-only; a multi-outcome candidate sitting at
    an extreme price is normal and the market should still be surfaced.
    """
    long_shot = {
        **MULTI_ROW,
        "outcomePrices": "[\"1.00\", \"0.00\", \"0.00\"]",
    }
    monkeypatch.setattr(markets.httpx, "get", lambda *a, **k: _make_resp([long_shot]))

    result = markets.fetch_active_markets(limit=10)
    assert len(result) == 1
    assert result[0].is_binary is False


def test_binary_market_still_parses_unchanged(monkeypatch):
    monkeypatch.setattr(markets.httpx, "get", lambda *a, **k: _make_resp([BINARY_ROW]))

    result = markets.fetch_active_markets(limit=10)
    assert len(result) == 1
    m = result[0]

    assert m.is_binary is True
    assert m.yes_price == 0.40
    assert m.no_price == 0.60
    assert markets.get_token_id(m, "YES") == "tok_yes"
    assert markets.get_token_id(m, "NO") == "tok_no"

    d = m.to_dict()
    assert d["is_binary"] is True
    assert d["yes_price"] == 0.40
    assert d["no_price"] == 0.60
    assert d["outcomes"] == [
        {"outcome": "Yes", "token_id": "tok_yes", "price": 0.40},
        {"outcome": "No", "token_id": "tok_no", "price": 0.60},
    ]


def test_get_token_id_does_not_crash_on_multi_outcome(monkeypatch):
    """get_token_id is binary-oriented but must not crash on N>2 tokens."""
    monkeypatch.setattr(markets.httpx, "get", lambda *a, **k: _make_resp([MULTI_ROW]))
    m = markets.fetch_market("0xmulti3")
    # No YES/NO side present -> returns None rather than raising.
    assert markets.get_token_id(m, "YES") is None
    # An actual candidate label resolves to its token.
    assert markets.get_token_id(m, "Alice") == "tok_a"


def test_single_token_market_is_dropped(monkeypatch):
    """A degenerate market with only one usable token is still dropped."""
    one_token = {
        "question": "Only one option?",
        "conditionId": "0xsolo",
        "outcomePrices": "[\"0.50\"]",
        "outcomes": "[\"Alice\"]",
        "clobTokenIds": "[\"tok_a\"]",
        "volume": "9000",
        "endDate": "2026-09-01",
        "active": True,
        "tags": ["other"],
    }
    monkeypatch.setattr(markets.httpx, "get", lambda *a, **k: _make_resp([one_token]))
    assert markets.fetch_active_markets(limit=10) == []
