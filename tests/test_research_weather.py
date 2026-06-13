import json
from pathlib import Path

import polymarket_mcp.research.weather as weather

FIX = Path(__file__).parent / "fixtures"


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
