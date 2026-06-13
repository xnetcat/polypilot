import importlib

import polymarket_mcp.research.economic as economic


def test_no_api_key_returns_error(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    import polymarket_mcp.config as cfg
    importlib.reload(cfg)
    importlib.reload(economic)
    result = economic.fetch_economic_data("Will CPI inflation exceed 3%?")
    assert "error" in result


def test_with_key_parses_series(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "fake")
    import polymarket_mcp.config as cfg
    importlib.reload(cfg)
    importlib.reload(economic)
    economic._CACHE.clear()

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"observations": [{"value": "3.2", "date": "2026-05-01"}]}

    monkeypatch.setattr(economic.httpx, "get", lambda *a, **k: R())
    result = economic.fetch_economic_data("Will CPI inflation exceed 3%?")
    assert result.get("latest_value") == 3.2 or "series_id" in result
    assert result["series_id"] == "CPIAUCSL"
    assert result["latest_value"] == 3.2


def test_with_key_no_matching_series(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "fake")
    import polymarket_mcp.config as cfg
    importlib.reload(cfg)
    importlib.reload(economic)
    economic._CACHE.clear()
    result = economic.fetch_economic_data("Will the Lakers win tonight?")
    assert "error" in result
