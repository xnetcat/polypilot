import json
from pathlib import Path

import polymarket_mcp.research.weather as weather

FIX = Path(__file__).parent / "fixtures"


def _clear_caches():
    weather._CACHE.clear()
    weather._GEOCODE_CACHE.clear()


def _stub_response(payload):
    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    return R()


def test_fetch_weather_returns_forecast(monkeypatch):
    weather._CACHE.clear()
    weather._GEOCODE_CACHE.clear()
    geo = json.loads((FIX / "openmeteo_geocode.json").read_text())
    fc = json.loads((FIX / "openmeteo_forecast.json").read_text())

    def fake_get(url, *a, **k):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return geo if "geocoding" in url else fc

        return R()

    monkeypatch.setattr(weather.httpx, "get", fake_get)

    result = weather.fetch_weather_forecast(
        "Highest temperature in Tokyo on June 20?")
    assert result["city"].lower().startswith("tokyo")
    assert "forecast_high_c" in result
    assert result["forecast_high_c"] is not None
    assert result["source"] == "open-meteo"


def test_fetch_weather_unparseable_returns_error(monkeypatch):
    weather._CACHE.clear()
    weather._GEOCODE_CACHE.clear()
    # No city in the question -> error, no HTTP call needed.
    result = weather.fetch_weather_forecast("Will the stock market crash?")
    assert "error" in result


def test_extract_weather_identity_exact():
    identity = weather.extract_weather_identity(
        "Will the high in Tokyo on June 20 be 24C?")
    assert identity is not None
    assert identity["city"] == "tokyo"
    assert identity["market_type"] == "exact_temperature"
    assert identity["target_temp"] == 24.0


# --------------------------------------------------------------------------- #
# City extraction is not limited to the catalog (broad coverage via geocoding)
# --------------------------------------------------------------------------- #
def test_extract_city_non_catalog_in_phrase_with_verb():
    # "exceed" used to break the "in <city>" capture; a non-catalog city must
    # still be pulled out and handed to the geocoder.
    assert weather._extract_city(
        "Will the high in Phoenix exceed 110F on June 20?") == "phoenix"
    assert weather._extract_city(
        "Will it be above 40C in Riyadh on July 1?") == "riyadh"


def test_extract_city_subject_position():
    # "Will <City> <verb> <temp>" — the city sits before the verb, not after "in".
    assert weather._extract_city("Will Lagos hit 35C tomorrow?") == "lagos"
    assert weather._extract_city("Will Reykjavik reach 15C by Friday?") == "reykjavik"


def test_extract_city_subject_requires_weather_cue():
    # Same shape but no weather cue -> must NOT resolve the subject to a city.
    assert weather._extract_city("Will Tesla beat earnings on Friday?") is None
    assert weather._extract_city("Will the stock market crash?") is None


def test_fetch_weather_geocodes_non_catalog_city(monkeypatch):
    # End-to-end: a city absent from CITY_COORDS resolves via the geocoder and
    # produces a forecast (proves coverage isn't limited to the catalog).
    _clear_caches()
    assert "lagos" not in weather.CITY_COORDS
    geo = {"results": [{"name": "Lagos", "latitude": 6.4541, "longitude": 3.3947,
                        "country_code": "NG"}]}
    fc = {"daily": {"time": ["2026-06-20"], "temperature_2m_max": [33.0],
                    "temperature_2m_min": [25.0], "precipitation_sum": [1.2]}}

    def fake_get(url, *a, **k):
        return _stub_response(geo if "geocoding" in url else fc)

    monkeypatch.setattr(weather.httpx, "get", fake_get)
    result = weather.fetch_weather_forecast("Will Lagos hit 35C on June 20?")
    assert result["city"].lower().startswith("lagos")
    assert result["source"] == "open-meteo"
    assert result["forecast_days"]


# --------------------------------------------------------------------------- #
# weather.gov — secondary US source
# --------------------------------------------------------------------------- #
def _route_weather_get(geo, openmeteo, points, forecast, alerts):
    """Build a fake httpx.get that routes by URL to the right fixture."""

    def fake_get(url, *a, **k):
        if "geocoding" in url:
            return _stub_response(geo)
        if "api.open-meteo.com" in url:
            return _stub_response(openmeteo)
        if "/points/" in url:
            return _stub_response(points)
        if "/alerts/active" in url:
            return _stub_response(alerts)
        if "gridpoints" in url or "forecast" in url:
            return _stub_response(forecast)
        return _stub_response(openmeteo)

    return fake_get


def test_us_city_gets_weathergov_field(monkeypatch):
    """A US city (catalog -> is_us) gains a populated `weathergov` field."""
    _clear_caches()
    openmeteo = json.loads((FIX / "openmeteo_forecast.json").read_text())
    points = json.loads((FIX / "weathergov_points.json").read_text())
    forecast = json.loads((FIX / "weathergov_forecast.json").read_text())
    alerts = json.loads((FIX / "weathergov_alerts.json").read_text())

    monkeypatch.setattr(
        weather.httpx, "get",
        _route_weather_get(None, openmeteo, points, forecast, alerts),
    )

    result = weather.fetch_weather_forecast(
        "Will the high in New York on June 20 be 32C?")

    assert result["city"].lower().startswith("new york")
    # Open-Meteo (primary) still works exactly as before.
    assert result["forecast_high_c"] is not None

    # Secondary weather.gov source is present and sane.
    assert "weathergov" in result
    wg = result["weathergov"]
    assert wg["source"] == "weather.gov"
    # June 20 daytime high in the fixture is 90F == 32.22C.
    assert wg["forecast_high_c"] is not None
    assert 31.0 <= wg["forecast_high_c"] <= 33.0
    # Overnight low is the coldest period (72F == 22.22C).
    assert wg["forecast_low_c"] is not None
    assert wg["forecast_low_c"] < wg["forecast_high_c"]
    # Active alert is surfaced when easily available.
    assert wg["alert"] == "Heat Advisory"
    # Primary source label is unchanged — weather.gov is purely additive.
    assert result["source"] == "open-meteo"
    assert wg["source_url"].startswith("https://api.weather.gov/")


def test_non_us_city_has_no_weathergov_field(monkeypatch):
    """A non-US city (Tokyo) must NOT call weather.gov nor gain the field."""
    _clear_caches()
    geo = json.loads((FIX / "openmeteo_geocode.json").read_text())
    openmeteo = json.loads((FIX / "openmeteo_forecast.json").read_text())

    def fake_get(url, *a, **k):
        # weather.gov must never be reached for a non-US location.
        assert "weather.gov" not in url, f"unexpected weather.gov call: {url}"
        return _stub_response(geo if "geocoding" in url else openmeteo)

    monkeypatch.setattr(weather.httpx, "get", fake_get)

    result = weather.fetch_weather_forecast(
        "Will the high in Tokyo on June 20 be 24C?")

    assert result["city"].lower().startswith("tokyo")
    assert result["forecast_high_c"] is not None
    assert result["source"] == "open-meteo"
    assert "weathergov" not in result


def test_us_city_weathergov_error_leaves_result_working(monkeypatch):
    """If weather.gov errors, the result is the plain Open-Meteo payload."""
    _clear_caches()
    openmeteo = json.loads((FIX / "openmeteo_forecast.json").read_text())

    def fake_get(url, *a, **k):
        if "weather.gov" in url:
            raise RuntimeError("weather.gov down")
        return _stub_response(openmeteo)

    monkeypatch.setattr(weather.httpx, "get", fake_get)

    result = weather.fetch_weather_forecast(
        "Will the high in Denver on June 20 be 30C?")

    # Open-Meteo still produced a forecast; no weathergov field on error.
    assert result["forecast_high_c"] is not None
    assert result["source"] == "open-meteo"
    assert "weathergov" not in result


def test_get_weathergov_research_parses_highs_and_lows(monkeypatch):
    """Direct unit test of the port: daytime highs / overnight lows in C."""
    _clear_caches()
    points = json.loads((FIX / "weathergov_points.json").read_text())
    forecast = json.loads((FIX / "weathergov_forecast.json").read_text())
    alerts = json.loads((FIX / "weathergov_alerts.json").read_text())

    def fake_get(url, *a, **k):
        if "/points/" in url:
            return _stub_response(points)
        if "/alerts/active" in url:
            return _stub_response(alerts)
        return _stub_response(forecast)

    monkeypatch.setattr(weather.httpx, "get", fake_get)

    out = weather._get_weathergov_research(
        "new york", (40.7128, -74.0060), "2026-06-20")
    assert out is not None
    assert out["source"] == "weather.gov"
    assert out["source_url"].startswith("https://api.weather.gov/")
    # 90F daytime -> 32.22C high for the target date.
    assert round(out["forecast_high_c"], 1) == 32.2
    assert out["alert"] == "Heat Advisory"
    # Per-day reduction keeps both a high and (where present) a low.
    by_date = {d["date"]: d for d in out["forecast_days"]}
    assert "2026-06-20" in by_date
    assert by_date["2026-06-20"]["high_c"] is not None


def test_get_weathergov_research_no_forecast_url_returns_none(monkeypatch):
    """A points response lacking a forecast URL yields None (never raises)."""
    _clear_caches()

    def fake_get(url, *a, **k):
        return _stub_response({"properties": {}})

    monkeypatch.setattr(weather.httpx, "get", fake_get)
    assert weather._get_weathergov_research("x", (1.0, 2.0), None) is None


def test_fahrenheit_to_c():
    assert round(weather._fahrenheit_to_c(32.0), 3) == 0.0
    assert round(weather._fahrenheit_to_c(212.0), 3) == 100.0
    assert round(weather._fahrenheit_to_c(90.0), 2) == 32.22
