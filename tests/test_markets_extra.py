"""Extra breadth coverage for polymarket_mcp.markets.

Tests assert CURRENT correct behavior and monkeypatch ``markets.httpx.get``.
"""
import json

import pytest

import polymarket_mcp.markets as markets


# ---------------------------------------------------------------------------
# Gamma -> CLOB fallback
# ---------------------------------------------------------------------------

CLOB_ROW = {
    "condition_id": "0xclob",
    "question": "Will the CLOB fallback parse correctly?",
    "volume": "8000",
    "active": True,
    "end_date_iso": "2026-07-01",
    "tokens": [
        {"token_id": "tok_yes", "outcome": "Yes", "price": "0.55"},
        {"token_id": "tok_no", "outcome": "No", "price": "0.45"},
    ],
    "tags": ["misc"],
}


def _make_resp(payload):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    return FakeResp()


def test_fetch_active_markets_falls_back_to_clob(monkeypatch):
    """Gamma URL raises -> fall back to CLOB /markets and parse from there."""
    gamma_url = f"{markets.config.GAMMA_API}/markets"
    clob_url = f"{markets.config.POLYMARKET_HOST}/markets"

    def fake_get(url, *args, **kwargs):
        if url == gamma_url:
            raise RuntimeError("gamma is down")
        if url == clob_url:
            return _make_resp([CLOB_ROW])
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(markets.httpx, "get", fake_get)

    result = markets.fetch_active_markets(limit=10)
    assert len(result) == 1
    m = result[0]
    assert m.condition_id == "0xclob"
    assert m.question == "Will the CLOB fallback parse correctly?"
    assert m.yes_price == 0.55
    assert m.no_price == 0.45
    assert m.volume == 8000.0
    # binary tokens preserved through the fallback path
    assert markets.get_token_id(m, "YES") == "tok_yes"
    assert markets.get_token_id(m, "NO") == "tok_no"


# ---------------------------------------------------------------------------
# _infer_category
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "question,expected",
    [
        ("Will Bitcoin hit $100k by year end?", "crypto"),
        ("Who will win the 2028 presidential election?", "politics"),
        ("Will Trump win the Iowa caucus?", "politics"),
        # "ukraine" contains the substring "rain" -- a real politics keyword
        # (Zelensky) must classify this as politics, NOT weather.
        ("Will Zelensky meet Putin in Ukraine?", "politics"),
        ("Will the highest temperature in NYC exceed 30°C tomorrow?", "weather"),
        ("Will the Lakers win their next NBA game?", "sports"),
        ("Lakers vs. Celtics: who wins?", "sports"),
        ("Will Team Vitality win the Counter-Strike Major?", "esports"),
        ("Will OpenAI release GPT-6 this year?", "ai"),
        ("Will the price of bananas in Peru change?", "other"),
    ],
)
def test_infer_category(question, expected):
    assert markets._infer_category(question, []) == expected


def test_infer_category_ukraine_is_not_weather():
    """Regression guard: a bare "ukraine" question must NEVER match weather.

    "ukraine" contains the substring "rain", but the weather keywords are the
    multi-word phrases "rain forecast"/"rainfall"/"precipitation", so a bare
    ukraine question (with no real politics keyword) currently falls through to
    "other" -- and crucially is not classified as weather.
    """
    category = markets._infer_category("Ukraine peace deal in 2026?", [])
    assert category != "weather"
    assert category == "other"


# ---------------------------------------------------------------------------
# get_token_id
# ---------------------------------------------------------------------------

def _binary_market():
    return markets.Market(
        condition_id="0xabc",
        question="Binary?",
        category="other",
        yes_price=0.5,
        no_price=0.5,
        volume=1000.0,
        end_date="2026-07-01",
        active=True,
        tokens=[
            {"token_id": "tok_yes", "outcome": "Yes", "price": 0.5},
            {"token_id": "tok_no", "outcome": "No", "price": 0.5},
        ],
    )


def test_get_token_id_no_side():
    assert markets.get_token_id(_binary_market(), "NO") == "tok_no"


def test_get_token_id_missing_side_returns_none():
    assert markets.get_token_id(_binary_market(), "MAYBE") is None


# ---------------------------------------------------------------------------
# _has_binary_tokens
# ---------------------------------------------------------------------------

def test_has_binary_tokens_false_for_non_binary():
    non_binary = [
        {"token_id": "a", "outcome": "Trump", "price": 0.5},
        {"token_id": "b", "outcome": "Biden", "price": 0.5},
    ]
    assert markets._has_binary_tokens(non_binary) is False


def test_non_binary_market_is_included_by_fetch(monkeypatch):
    """A market whose outcomes are not YES/NO is now surfaced as multi-outcome.

    Multi-outcome markets (>=2 usable tokens) pass discovery; the strict YES/NO
    gate was removed. The Market is flagged ``is_binary=False`` and exposes all
    candidate tokens.
    """
    non_binary_row = {
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
    monkeypatch.setattr(markets.httpx, "get", lambda *a, **k: _make_resp([non_binary_row]))
    result = markets.fetch_active_markets(limit=10)
    assert len(result) == 1
    m = result[0]
    assert m.condition_id == "0xmulti"
    assert m.is_binary is False
    assert {t["outcome"] for t in m.tokens} == {"Trump", "Biden"}


# ---------------------------------------------------------------------------
# Market.to_dict
# ---------------------------------------------------------------------------

def test_to_dict_documented_keys_and_json_serializable():
    d = _binary_market().to_dict()
    expected_keys = {
        "market_id",
        "question",
        "category",
        "yes_price",
        "no_price",
        "volume",
        "end_date",
        "active",
        "tokens",
        "is_binary",
        "outcomes",
    }
    assert set(d.keys()) == expected_keys
    assert d["market_id"] == "0xabc"
    assert d["is_binary"] is True
    # outcomes mirror every token (label/token_id/price)
    assert d["outcomes"] == [
        {"outcome": "Yes", "token_id": "tok_yes", "price": 0.5},
        {"outcome": "No", "token_id": "tok_no", "price": 0.5},
    ]
    # must be JSON-serializable (round-trips cleanly)
    assert json.loads(json.dumps(d)) == d
