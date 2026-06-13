"""Breadth tests for the research helpers (weather/crypto/sports/economic/news).

These cover cases NOT already exercised by the per-module test files:
- weather: directional threshold (above vs below) vs exact vs range parsing,
  asserted directly against the ``_extract_weather_target`` helper.
- crypto: ETH/SOL symbol detection, numeric "$70,000" target parsing, and the
  no-asset error path.
- sports: ``_detect_sport`` for NBA / NFL / soccer plus team extraction.
- economic: FRED series mapping for CPI/GDP/unemployment/rates, and the
  no-API-key path returning an error WITHOUT making an HTTP call.
- news: ``deduplicate`` collapsing near-identical headlines, plus ``fetch_news``
  topic filtering, ``limit`` honouring, and newest-first sorting.

All tests assert current correct behavior and run fully offline (httpx /
internal helpers are monkeypatched, or pure parsing helpers are called direct).
"""
from __future__ import annotations

import importlib

import pytest

import polymarket_mcp.research.crypto as crypto
import polymarket_mcp.research.economic as economic
import polymarket_mcp.research.news as news
import polymarket_mcp.research.sports as sports
import polymarket_mcp.research.weather as weather


# --------------------------------------------------------------------------- #
# weather — temperature-target direction parsing
# --------------------------------------------------------------------------- #
def test_weather_target_threshold_above():
    """'X°C or higher' -> threshold_above / direction above."""
    target = weather._extract_weather_target(
        "Will the high in Tokyo be 25C or higher on June 20?")
    assert target["market_type"] == "threshold_above"
    assert target["threshold_direction"] == "above"
    assert target["target_temp"] == 25.0
    assert target["target_unit"] == "C"


def test_weather_target_threshold_below():
    """'X°C or lower' -> threshold_below / direction below (distinct from above)."""
    target = weather._extract_weather_target(
        "Will the high in Tokyo be 25C or lower on June 20?")
    assert target["market_type"] == "threshold_below"
    assert target["threshold_direction"] == "below"
    assert target["target_temp"] == 25.0


def test_weather_target_above_and_below_differ():
    """The two directional phrasings must NOT collapse to the same market_type."""
    above = weather._extract_weather_target("high of 25C or above?")
    below = weather._extract_weather_target("high of 25C or below?")
    assert above["market_type"] == "threshold_above"
    assert below["market_type"] == "threshold_below"
    assert above["market_type"] != below["market_type"]
    assert above["threshold_direction"] != below["threshold_direction"]


def test_weather_target_exact_when_no_direction_keyword():
    """A plain 'highest temperature X°C' with no above/below word -> exact."""
    target = weather._extract_weather_target(
        "Highest temperature in Tokyo on June 20 25C?")
    assert target["market_type"] == "exact_temperature"
    assert target["target_temp"] == 25.0
    # An exact-temperature target carries no directional field.
    assert "threshold_direction" not in target


def test_weather_target_range_between():
    """'between X and Y' -> range_temperature with sorted low/high bounds."""
    target = weather._extract_weather_target(
        "Will the high in Tokyo be between 20 and 25C on June 20?")
    assert target["market_type"] == "range_temperature"
    assert target["target_range_low"] == 20.0
    assert target["target_range_high"] == 25.0
    assert target["target_range_low_c"] == 20.0
    assert target["target_range_high_c"] == 25.0
    assert target["target_range_midpoint_c"] == 22.5


def test_weather_target_leading_above_keyword():
    """A bare 'above 25C' (keyword before the number) still parses as above."""
    target = weather._extract_weather_target("Will the high be above 25C?")
    assert target["market_type"] == "threshold_above"
    assert target["threshold_direction"] == "above"


def test_weather_identity_rejects_threshold():
    """extract_weather_identity only accepts exact-temperature markets."""
    identity = weather.extract_weather_identity(
        "Will the high in Tokyo on June 20 be 25C or higher?")
    assert identity is None


# --------------------------------------------------------------------------- #
# crypto — symbol detection / numeric target / no-asset error
# --------------------------------------------------------------------------- #
def _crypto_stub(payload):
    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    return lambda *a, **k: R()


def test_crypto_detects_eth(monkeypatch):
    crypto._CACHE.clear()
    monkeypatch.setattr(
        crypto.httpx, "get",
        _crypto_stub({"ethereum": {"usd": 3500, "usd_24h_change": 1.2,
                                   "usd_24h_vol": 9.0}}),
    )
    result = crypto.fetch_crypto("Will Ethereum end the week higher?")
    assert result["symbol"] == "ETH"
    assert result["coin_id"] == "ethereum"
    assert result["spot_usd"] == 3500.0


def test_crypto_detects_sol_by_ticker(monkeypatch):
    crypto._CACHE.clear()
    monkeypatch.setattr(
        crypto.httpx, "get",
        _crypto_stub({"solana": {"usd": 150, "usd_24h_change": -2.0,
                                 "usd_24h_vol": 3.0}}),
    )
    result = crypto.fetch_crypto("Will SOL rally this month?")
    assert result["symbol"] == "SOL"
    assert result["coin_id"] == "solana"


def test_crypto_parses_comma_grouped_target(monkeypatch):
    """A '$70,000' target is parsed (commas stripped) with direction above."""
    crypto._CACHE.clear()
    monkeypatch.setattr(
        crypto.httpx, "get",
        _crypto_stub({"ethereum": {"usd": 3500, "usd_24h_change": 0.0,
                                   "usd_24h_vol": 1.0}}),
    )
    result = crypto.fetch_crypto("Will Ethereum be above $70,000?")
    assert "target" in result
    assert result["target"]["price"] == 70000.0
    assert result["target"]["direction"] == "above"
    assert result["market_type"] == "threshold"


def test_crypto_no_asset_returns_error():
    crypto._CACHE.clear()
    result = crypto.fetch_crypto("Will it snow in Denver next week?")
    assert "error" in result
    assert "symbol" not in result


# --------------------------------------------------------------------------- #
# sports — sport detection + team extraction
# --------------------------------------------------------------------------- #
def test_sports_detect_nba():
    assert sports._detect_sport("Will the Lakers cover tonight?") == "basketball/nba"


def test_sports_detect_nfl():
    assert sports._detect_sport("NFL: Chiefs vs Eagles, who wins?") == "football/nfl"


def test_sports_detect_soccer_league_keyword():
    """A league keyword maps straight to the ESPN soccer slug."""
    assert (
        sports._detect_sport("Will Arsenal top the Premier League table?")
        == "soccer/eng.1"
    )


def test_sports_detect_soccer_by_team():
    """A bare soccer club name maps to its league even without a keyword."""
    assert sports._detect_sport("Bayern vs Dortmund this weekend?") == "soccer/ger.1"


def test_sports_extract_teams_vs():
    teams = sports._extract_teams_from_question("Team A vs Team B")
    assert teams == ("Team A", "Team B")


def test_sports_extract_teams_beat():
    teams = sports._extract_teams_from_question("Will the Lakers beat the Celtics?")
    assert teams == ("the Lakers", "the Celtics")


# --------------------------------------------------------------------------- #
# economic — FRED series mapping + no-key error path (no HTTP call)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "question, series_id",
    [
        ("Will CPI inflation exceed 3%?", "CPIAUCSL"),
        ("Will real GDP growth beat expectations?", "GDP"),
        ("Will the unemployment rate fall?", "UNRATE"),
        ("Will the Fed cut interest rates?", "FEDFUNDS"),
    ],
)
def test_economic_series_detection(question, series_id):
    detected = economic._detect_macro_series(question)
    assert detected is not None
    assert detected["series_id"] == series_id


def test_economic_no_key_returns_error_without_http(monkeypatch):
    """With no FRED_API_KEY, fetch_economic_data errors and never calls httpx."""
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    import polymarket_mcp.config as cfg

    importlib.reload(cfg)
    importlib.reload(economic)
    economic._CACHE.clear()

    def _boom(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("httpx.get must not be called when FRED_API_KEY is unset")

    monkeypatch.setattr(economic.httpx, "get", _boom)
    result = economic.fetch_economic_data("Will CPI inflation exceed 3%?")
    assert "error" in result
    assert "FRED_API_KEY" in result["error"]

    # Restore a clean module state for any later test in the session.
    importlib.reload(cfg)
    importlib.reload(economic)


# --------------------------------------------------------------------------- #
# news — deduplicate + fetch_news topic/limit/sort
# --------------------------------------------------------------------------- #
def test_news_deduplicate_collapses_near_identical():
    """Headlines sharing the same normalized 80-char prefix collapse to one.

    deduplicate keys on ``headline.lower()[:80]``, so two headlines that agree
    on their first 80 characters but diverge only in a trailing suffix are
    treated as duplicates (the differing tail is truncated away).
    """
    # 90+ chars so the differing suffix lands past the 80-char key boundary.
    base = ("Federal Reserve holds interest rates steady amid mixed economic "
            "signals and cautious market outlook")
    assert len(base) > 80
    items = [
        news.NewsItem(headline=base, source="a", published="t1", url="u1"),
        news.NewsItem(headline=base + " (updated)", source="b",
                      published="t2", url="u2"),
        news.NewsItem(headline="Completely different story", source="c",
                      published="t3", url="u3"),
    ]
    unique = news.deduplicate(items)
    assert len(unique) == 2
    # First-seen wins for the collapsed pair.
    assert unique[0].url == "u1"
    assert unique[1].headline == "Completely different story"


def test_news_fetch_filters_sorts_and_limits(monkeypatch):
    """fetch_news filters by topic substring, sorts newest-first, honours limit."""
    sample = [
        news.NewsItem(headline="Bitcoin dips below support",
                      source="x", published="2026-06-10T08:00:00+00:00", url="b1"),
        news.NewsItem(headline="Bitcoin rallies to new high",
                      source="x", published="2026-06-13T08:00:00+00:00", url="b2"),
        news.NewsItem(headline="Bitcoin ETF inflows surge",
                      source="x", published="2026-06-12T08:00:00+00:00", url="b3"),
        news.NewsItem(headline="City council debates parking",
                      source="y", published="2026-06-13T09:00:00+00:00", url="o1"),
    ]
    monkeypatch.setattr(news, "_gather_rss", lambda *a, **k: list(sample))
    monkeypatch.setattr(news.config, "NEWSAPI_KEY", "")

    result = news.fetch_news("bitcoin", limit=2)
    # Only the 3 bitcoin items survive the topic filter; limit=2 trims to 2.
    assert result["count"] == 2
    assert len(result["headlines"]) == 2
    # Newest-first ordering: 06-13 then 06-12.
    assert result["headlines"][0]["url"] == "b2"
    assert result["headlines"][1]["url"] == "b3"
    # The non-matching headline is excluded entirely.
    assert all("bitcoin" in h["headline"].lower() for h in result["headlines"])


def test_news_empty_topic_returns_all(monkeypatch):
    """An empty topic skips filtering and returns everything (up to limit)."""
    sample = [
        news.NewsItem(headline="Story one", source="x",
                      published="2026-06-11T00:00:00+00:00", url="s1"),
        news.NewsItem(headline="Story two", source="x",
                      published="2026-06-12T00:00:00+00:00", url="s2"),
    ]
    monkeypatch.setattr(news, "_gather_rss", lambda *a, **k: list(sample))
    monkeypatch.setattr(news.config, "NEWSAPI_KEY", "")
    result = news.fetch_news("", limit=10)
    assert result["count"] == 2
