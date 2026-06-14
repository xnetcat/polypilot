"""Enhancement tests for the research package.

Covers the two enhancements layered on top of the ported research helpers:

1. **Cache TTL** (``polymarket_mcp.research._cache``): each research module keeps
   a module-level ``_CACHE`` keyed by question text. Without expiry a
   long-running trading loop would stale-serve forecasts/prices forever. These
   tests assert a cache HIT returns the *same* object within the TTL window but
   RE-FETCHES once the entry expires — forced both by a zero TTL and by
   monkeypatching the monotonic clock.

2. **Sports team normalization** (``research.sports``): the team extractor used
   to keep trailing time/filler words ("tonight", "game", ...) and leading
   articles ("the Lakers"), which hurt the ESPN 6-char-prefix match. The
   extractor now strips trailing noise, and ``_normalize_team_name`` strips the
   leading article for the ESPN match keys. Clean "Team A vs Team B" still
   parses unchanged.

All network access (``httpx.get``) is monkeypatched; nothing here touches the
real Open-Meteo / CoinGecko / ESPN endpoints.
"""
from __future__ import annotations

import polymarket_mcp.research._cache as _cache
import polymarket_mcp.research.crypto as crypto
import polymarket_mcp.research.sports as sports
import polymarket_mcp.research.weather as weather


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class _Resp:
    """Minimal stand-in for an httpx.Response carrying a fixed JSON payload."""

    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


def _counting_get(payload_for_call):
    """Build a fake ``httpx.get`` that records its call count.

    ``payload_for_call`` receives the (1-based) call index and the request URL
    and returns the JSON payload for that call, so a test can hand back
    *different* data on a re-fetch and observe the change.
    """
    state = {"calls": 0}

    def fake_get(url, *args, **kwargs):
        state["calls"] += 1
        return _Resp(payload_for_call(state["calls"], url))

    fake_get.state = state
    return fake_get


# --------------------------------------------------------------------------- #
# _cache — direct unit tests of the TTL primitive
# --------------------------------------------------------------------------- #
def test_cache_hit_within_ttl_returns_same_object():
    store: dict = {}
    value = {"x": 1}
    _cache.cache_set(store, "k", value)
    hit = _cache.cache_get(store, "k", ttl=600)
    assert hit is value  # identity preserved on a hit


def test_cache_zero_ttl_is_always_a_miss():
    store: dict = {}
    _cache.cache_set(store, "k", {"x": 1})
    assert _cache.cache_get(store, "k", ttl=0) is _cache._MISS
    # The expired entry is evicted on access.
    assert "k" not in store


def test_cache_expires_via_clock(monkeypatch):
    store: dict = {}
    clock = {"t": 1000.0}
    monkeypatch.setattr(_cache, "_now", lambda: clock["t"])
    _cache.cache_set(store, "k", {"x": 1})

    # Still inside the 600s window -> hit.
    clock["t"] = 1000.0 + 599.0
    assert _cache.cache_get(store, "k", ttl=600) is not _cache._MISS

    # Past the window -> miss + eviction.
    clock["t"] = 1000.0 + 601.0
    assert _cache.cache_get(store, "k", ttl=600) is _cache._MISS
    assert "k" not in store


def test_cache_get_missing_key_returns_default():
    store: dict = {}
    sentinel = object()
    assert _cache.cache_get(store, "absent", default=sentinel) is sentinel


# --------------------------------------------------------------------------- #
# weather — cache hit within TTL vs re-fetch after expiry
# --------------------------------------------------------------------------- #
def _weather_payload_for(call_idx: int, url: str, high: float) -> dict:
    """Build geocode/forecast payloads; the forecast high is parameterized so a
    re-fetch is observable in the returned ``forecast_high_c``."""
    if "geocoding" in url:
        return {"results": [{
            "name": "Tokyo", "latitude": 35.6762, "longitude": 139.6503,
            "country_code": "JP",
        }]}
    return {"daily": {
        "time": ["2026-06-20"],
        "temperature_2m_max": [high],
        "temperature_2m_min": [18.0],
        "precipitation_sum": [0.0],
        "weathercode": [1],
    }}


def test_weather_cache_hit_within_ttl(monkeypatch):
    weather._CACHE.clear()
    weather._GEOCODE_CACHE.clear()
    # Tokyo is in the catalog, so only the forecast endpoint is hit.
    fake = _counting_get(lambda n, url: _weather_payload_for(n, url, 24.0))
    monkeypatch.setattr(weather.httpx, "get", fake)
    monkeypatch.setattr(weather, "CACHE_TTL_SECONDS", 600.0)

    q = "Highest temperature in Tokyo on June 20?"
    first = weather.fetch_weather_forecast(q)
    calls_after_first = fake.state["calls"]
    assert calls_after_first >= 1
    assert first["forecast_high_c"] == 24.0

    # Second call within TTL: no new HTTP fetch, identical result.
    second = weather.fetch_weather_forecast(q)
    assert fake.state["calls"] == calls_after_first  # no re-fetch
    assert second["forecast_high_c"] == 24.0


def test_weather_cache_refetches_after_expiry_zero_ttl(monkeypatch):
    weather._CACHE.clear()
    weather._GEOCODE_CACHE.clear()
    # First call yields 24C, every later call yields 30C so a re-fetch shows up.
    fake = _counting_get(
        lambda n, url: _weather_payload_for(n, url, 24.0 if n <= 1 else 30.0)
    )
    monkeypatch.setattr(weather.httpx, "get", fake)
    # ttl=0 forces every cached entry to be treated as expired -> always re-fetch.
    monkeypatch.setattr(weather, "CACHE_TTL_SECONDS", 0.0)

    q = "Highest temperature in Tokyo on June 20?"
    first = weather.fetch_weather_forecast(q)
    calls_after_first = fake.state["calls"]
    assert first["forecast_high_c"] == 24.0

    second = weather.fetch_weather_forecast(q)
    assert fake.state["calls"] > calls_after_first  # re-fetched
    assert second["forecast_high_c"] == 30.0  # fresh data served


def test_weather_cache_refetches_after_clock_advance(monkeypatch):
    weather._CACHE.clear()
    weather._GEOCODE_CACHE.clear()
    clock = {"t": 5000.0}
    monkeypatch.setattr(_cache, "_now", lambda: clock["t"])
    fake = _counting_get(
        lambda n, url: _weather_payload_for(n, url, 24.0 if n <= 1 else 31.0)
    )
    monkeypatch.setattr(weather.httpx, "get", fake)
    monkeypatch.setattr(weather, "CACHE_TTL_SECONDS", 600.0)

    q = "Highest temperature in Tokyo on June 20?"
    first = weather.fetch_weather_forecast(q)
    assert first["forecast_high_c"] == 24.0
    calls_after_first = fake.state["calls"]

    # Within TTL: served from cache, no new fetch.
    clock["t"] += 100.0
    weather.fetch_weather_forecast(q)
    assert fake.state["calls"] == calls_after_first

    # Advance past the TTL window: entry expires and is re-fetched.
    clock["t"] += 600.0
    third = weather.fetch_weather_forecast(q)
    assert fake.state["calls"] > calls_after_first
    assert third["forecast_high_c"] == 31.0


# --------------------------------------------------------------------------- #
# crypto — cache hit within TTL vs re-fetch after expiry
# --------------------------------------------------------------------------- #
def _crypto_payload(price: float) -> dict:
    return {"bitcoin": {
        "usd": price, "usd_24h_change": 0.5, "usd_24h_vol": 1.0,
    }}


def test_crypto_cache_hit_within_ttl(monkeypatch):
    crypto._CACHE.clear()
    fake = _counting_get(lambda n, url: _crypto_payload(60000.0))
    monkeypatch.setattr(crypto.httpx, "get", fake)
    monkeypatch.setattr(crypto, "CACHE_TTL_SECONDS", 600.0)

    q = "Will Bitcoin go up or down today?"
    first = crypto.fetch_crypto(q)
    assert first["spot_usd"] == 60000.0
    calls_after_first = fake.state["calls"]
    assert calls_after_first == 1

    second = crypto.fetch_crypto(q)
    assert fake.state["calls"] == 1  # no re-fetch within TTL
    assert second["spot_usd"] == 60000.0


def test_crypto_cache_refetches_after_expiry_zero_ttl(monkeypatch):
    crypto._CACHE.clear()
    fake = _counting_get(
        lambda n, url: _crypto_payload(60000.0 if n <= 1 else 65000.0)
    )
    monkeypatch.setattr(crypto.httpx, "get", fake)
    monkeypatch.setattr(crypto, "CACHE_TTL_SECONDS", 0.0)

    q = "Will Bitcoin go up or down today?"
    first = crypto.fetch_crypto(q)
    assert first["spot_usd"] == 60000.0

    second = crypto.fetch_crypto(q)
    assert fake.state["calls"] == 2  # re-fetched on expiry
    assert second["spot_usd"] == 65000.0  # fresh price served


def test_crypto_cache_refetches_after_clock_advance(monkeypatch):
    crypto._CACHE.clear()
    clock = {"t": 0.0}
    monkeypatch.setattr(_cache, "_now", lambda: clock["t"])
    fake = _counting_get(
        lambda n, url: _crypto_payload(60000.0 if n <= 1 else 70000.0)
    )
    monkeypatch.setattr(crypto.httpx, "get", fake)
    monkeypatch.setattr(crypto, "CACHE_TTL_SECONDS", 600.0)

    q = "Will Bitcoin go up or down today?"
    assert crypto.fetch_crypto(q)["spot_usd"] == 60000.0

    clock["t"] = 300.0  # within TTL
    assert crypto.fetch_crypto(q)["spot_usd"] == 60000.0
    assert fake.state["calls"] == 1

    clock["t"] = 1200.0  # past TTL
    assert crypto.fetch_crypto(q)["spot_usd"] == 70000.0
    assert fake.state["calls"] == 2


# --------------------------------------------------------------------------- #
# sports — team name normalization
# --------------------------------------------------------------------------- #
def test_normalize_team_strips_leading_article():
    assert sports._normalize_team_name("the Lakers") == "Lakers"
    assert sports._normalize_team_name("the Celtics") == "Celtics"


def test_normalize_team_strips_trailing_time_words():
    assert sports._normalize_team_name("the Celtics tonight") == "Celtics"
    assert sports._normalize_team_name("Lakers today") == "Lakers"
    assert sports._normalize_team_name("Warriors game") == "Warriors"
    assert sports._normalize_team_name("the Heat tomorrow") == "Heat"


def test_normalize_team_strips_trailing_punctuation_and_whitespace():
    assert sports._normalize_team_name("  Lakers!  ") == "Lakers"
    assert sports._normalize_team_name("Real   Madrid") == "Real Madrid"


def test_normalize_team_preserves_multiword_names():
    # Multi-word team names must not be truncated by the noise trimmer.
    assert sports._normalize_team_name("the Maple Leafs") == "Maple Leafs"
    assert sports._normalize_team_name("Trail Blazers") == "Trail Blazers"
    assert sports._normalize_team_name("the Golden Knights") == "Golden Knights"


def test_normalize_team_can_keep_article_when_requested():
    # The public extractor keeps the article in its display name.
    assert sports._normalize_team_name("the Lakers", strip_article=False) == "the Lakers"
    # ...but still strips trailing time/filler noise.
    assert (
        sports._normalize_team_name("the Lakers tonight", strip_article=False)
        == "the Lakers"
    )


def test_extract_teams_strips_trailing_tonight():
    """'... the Celtics tonight?' drops the trailing time word from team2."""
    teams = sports._extract_teams_from_question(
        "Will the Lakers beat the Celtics tonight?")
    assert teams is not None
    team1, team2 = teams
    # Trailing "tonight" is stripped (the prior behavior leaked it into team2),
    # and the leading article is stripped too.
    assert "tonight" not in team2.lower()
    assert team2 == "Celtics"
    assert team1 == "Lakers"


def test_extract_teams_clean_vs_unchanged():
    """A clean 'Team A vs Team B' is parsed without article/noise mangling."""
    assert sports._extract_teams_from_question("Lakers vs Celtics") == (
        "Lakers", "Celtics")
    assert sports._extract_teams_from_question("Bayern vs Dortmund") == (
        "Bayern", "Dortmund")


def test_extract_teams_vs_with_trailing_tonight():
    """Trailing time word after the second team in a 'vs' question is stripped."""
    teams = sports._extract_teams_from_question("Lakers vs Celtics tonight")
    assert teams == ("Lakers", "Celtics")


def _espn_two_team_payload(home_name: str, away_name: str) -> dict:
    """Minimal ESPN scoreboard payload with one scheduled two-team event."""
    return {"events": [{
        "name": f"{away_name} at {home_name}",
        "shortName": "AWY @ HOM",
        "competitions": [{
            "status": {"type": {"state": "pre", "detail": "Scheduled"}},
            "competitors": [
                {"team": {"displayName": home_name}, "score": "0",
                 "homeAway": "home"},
                {"team": {"displayName": away_name}, "score": "0",
                 "homeAway": "away"},
            ],
        }],
    }]}


def test_espn_match_succeeds_despite_article_and_tonight(monkeypatch):
    """The article+filler strip lets the ESPN 6-char prefix match actually hit.

    With the old behavior team1 "the Lakers" keyed on "the La", which never
    appears in the ESPN event name "Boston Celtics at Los Angeles Lakers", so
    the match failed. After normalization the key is "lakers" and the event is
    found.
    """
    sports._CACHE.clear()
    sports._SCOREBOARD_CACHE.clear()
    payload = _espn_two_team_payload("Los Angeles Lakers", "Boston Celtics")
    # League is unknown (no keyword), so the dynamic probe walks leagues; serve
    # the game only under the NBA slug so it resolves to the correct league.
    monkeypatch.setattr(sports.httpx, "get",
        lambda url, *a, **k: _Resp(payload if "basketball/nba" in url else {"events": []}))

    result = sports.fetch_sports("Will the Lakers beat the Celtics tonight?")
    assert result.get("error") is None
    assert result["sport"] == "basketball/nba"
    # The event was matched and a score structure surfaced.
    assert result["status"] in ("pre", "in", "post")
    assert result["score"] is not None
    assert len(result["score"]) == 2
