import importlib

import polymarket_mcp.research.economic as economic


def _reload_with_key(monkeypatch, key="fake"):
    """Reload config + economic with FRED_API_KEY set (or unset) and a clean cache."""
    if key is None:
        monkeypatch.delenv("FRED_API_KEY", raising=False)
    else:
        monkeypatch.setenv("FRED_API_KEY", key)
    import polymarket_mcp.config as cfg
    importlib.reload(cfg)
    importlib.reload(economic)
    economic._CACHE.clear()


class _Resp:
    """Minimal httpx-like response wrapping a fixed JSON payload."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _route(fred=None, bls=None):
    """Build an httpx.get replacement that routes by URL host.

    ``fred``/``bls`` are JSON payloads (or callables raising) returned for the
    matching host. A None payload for a host raises, simulating an error.
    """

    def _get(url, *args, **kwargs):
        if "stlouisfed.org" in url:
            if fred is None:
                raise RuntimeError("FRED unavailable")
            return _Resp(fred)
        if "bls.gov" in url:
            if bls is None:
                raise RuntimeError("BLS unavailable")
            return _Resp(bls)
        raise AssertionError(f"unexpected URL: {url}")

    return _get


def _fred_payload(observations):
    return {"observations": observations}


def _bls_payload(value, year="2026", period="M05", period_name="May"):
    return {
        "status": "REQUEST_SUCCEEDED",
        "Results": {
            "series": [
                {
                    "seriesID": "X",
                    "data": [
                        {"year": year, "period": period,
                         "periodName": period_name, "value": value}
                    ],
                }
            ]
        },
    }


def test_no_api_key_returns_error(monkeypatch):
    _reload_with_key(monkeypatch, key=None)
    result = economic.fetch_economic_data("Will GDP growth exceed 3%?")
    assert "error" in result


def test_with_key_parses_series(monkeypatch):
    _reload_with_key(monkeypatch)
    # BLS is now ALSO called for CPI; route it to a stub so the FRED assertions hold.
    monkeypatch.setattr(
        economic.httpx,
        "get",
        _route(
            fred=_fred_payload([{"value": "3.2", "date": "2026-05-01"}]),
            bls=_bls_payload("3.2"),
        ),
    )
    result = economic.fetch_economic_data("Will CPI inflation exceed 3%?")
    assert result.get("latest_value") == 3.2 or "series_id" in result
    assert result["series_id"] == "CPIAUCSL"
    assert result["latest_value"] == 3.2


def test_with_key_no_matching_series(monkeypatch):
    _reload_with_key(monkeypatch)
    result = economic.fetch_economic_data("Will the Lakers win tonight?")
    assert "error" in result


def test_cpi_both_sources_agree(monkeypatch):
    """CPI with FRED+BLS both present and agreeing -> sources has both,
    sources_agree True, value_divergence_pct small."""
    _reload_with_key(monkeypatch)
    monkeypatch.setattr(
        economic.httpx,
        "get",
        _route(
            fred=_fred_payload([
                {"value": "333.979", "date": "2026-05-01"},
                {"value": "332.000", "date": "2026-04-01"},
            ]),
            bls=_bls_payload("333.979"),
        ),
    )
    result = economic.fetch_economic_data("Will CPI inflation exceed 3%?")
    assert result["series_id"] == "CPIAUCSL"
    assert result["latest_value"] == 333.979
    assert result["source"] == "fred"
    sources = result["sources"]
    assert sources["fred"]["latest_value"] == 333.979
    assert sources["bls"]["latest_value"] == 333.979
    assert sources["bls"]["latest_date"] == "2026-M05"
    assert result["sources_agree"] is True
    assert result["value_divergence_pct"] == 0.0


def test_non_bls_series_has_no_bls_source(monkeypatch):
    """A non-BLS series (GDP) -> no `bls` key in sources, BLS never called."""
    _reload_with_key(monkeypatch)
    monkeypatch.setattr(
        economic.httpx,
        "get",
        _route(
            fred=_fred_payload([{"value": "29000.0", "date": "2026-01-01"}]),
            bls=None,  # would raise if called
        ),
    )
    result = economic.fetch_economic_data("Will GDP exceed $29 trillion?")
    assert result["series_id"] == "GDP"
    assert "bls" not in result["sources"]
    assert result["sources"]["fred"]["latest_value"] == 29000.0
    assert result["value_divergence_pct"] is None
    assert result["sources_agree"] is None


def test_bls_error_leaves_fred_intact(monkeypatch):
    """BLS error -> FRED result intact, BLS marked unavailable, no crash."""
    _reload_with_key(monkeypatch)
    monkeypatch.setattr(
        economic.httpx,
        "get",
        _route(
            fred=_fred_payload([{"value": "4.1", "date": "2026-05-01"}]),
            bls=None,  # raises -> _fetch_bls returns None
        ),
    )
    result = economic.fetch_economic_data("Will the unemployment rate stay below 5%?")
    assert result["series_id"] == "UNRATE"
    assert result["latest_value"] == 4.1
    assert result["source"] == "fred"
    assert result["sources"]["fred"]["latest_value"] == 4.1
    assert result["sources"]["bls"] == {"error": "unavailable"}
    assert result["value_divergence_pct"] is None
    assert result["sources_agree"] is None


def test_bls_fallback_when_fred_unavailable(monkeypatch):
    """No FRED key but a BLS-backed series -> BLS supplies the top-level value."""
    _reload_with_key(monkeypatch, key=None)
    monkeypatch.setattr(
        economic.httpx,
        "get",
        _route(
            fred=None,  # FRED key unset -> _fetch_fred returns None anyway
            bls=_bls_payload("333.979"),
        ),
    )
    result = economic.fetch_economic_data("Will CPI inflation exceed 3%?")
    assert result["series_id"] == "CPIAUCSL"
    assert result["latest_value"] == 333.979
    assert result["latest_date"] == "2026-M05"
    assert result["source"] == "bls"
    assert result["sources"]["fred"] == {"error": "unavailable"}
    assert result["sources"]["bls"]["latest_value"] == 333.979
    # Only one source resolved -> no divergence / agreement.
    assert result["value_divergence_pct"] is None
    assert result["sources_agree"] is None
