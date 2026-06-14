"""Data-source diagnostics — which research APIs are configured and reachable.

A best-effort health check over every external source the research tools use, so
the agent (and the operator) can see at a glance what is actually available:
which optional keys are set, and — when ``probe`` is on — whether each endpoint
responds. Mirrors the predecessor's ``verify`` command. Never raises; every probe
is wrapped in try/except and bounded by a short timeout.
"""
from __future__ import annotations

import logging

import httpx

from . import config

log = logging.getLogger(__name__)

_PROBE_TIMEOUT = 6.0
# weather.gov rejects requests without a User-Agent; send a polite one.
_WEATHER_GOV_UA = "polypilot-mcp (data-source diagnostics)"


def _probe(url: str, headers: dict | None = None) -> dict:
    """GET ``url`` and report reachability. Never raises.

    ``reachable`` is True on a 2xx response, False otherwise (including network
    errors). ``status`` carries the HTTP code, or a short error string.
    """
    try:
        resp = httpx.get(url, headers=headers or {}, timeout=_PROBE_TIMEOUT,
                         follow_redirects=True)
        return {"reachable": 200 <= resp.status_code < 300, "status": resp.status_code}
    except Exception as e:  # noqa: BLE001 — best-effort, never propagate.
        log.debug("[diagnostics] probe error for %s: %s", url, e)
        return {"reachable": False, "status": str(e)[:200]}


def check_data_sources(probe: bool = True) -> dict:
    """Report each research data source: key requirement, config, reachability.

    With ``probe=True`` (default) each source whose prerequisites are met is
    pinged with a lightweight request; keyed sources without a configured key are
    reported as ``key_configured: false`` and are NOT probed (``reachable: null``).
    With ``probe=False`` no network calls are made — only the configuration is
    reported. Returns ``{mode, probed, sources: [...], summary: {...}}``.
    """
    sources: list[dict] = []

    # (name, note, requires_key, key_value, probe_url, probe_headers)
    specs: list[tuple] = [
        ("coingecko", "crypto spot / history / band-probability", False, "",
         "https://api.coingecko.com/api/v3/ping", None),
        ("binance", "crypto fallback when CoinGecko is rate-limited", False, "",
         "https://api.binance.com/api/v3/ping", None),
        ("open-meteo", "weather forecasts + city geocoding", False, "",
         "https://geocoding-api.open-meteo.com/v1/search?name=London&count=1", None),
        ("weather.gov", "secondary US weather source", False, "",
         "https://api.weather.gov/points/40.71,-74.01",
         {"User-Agent": _WEATHER_GOV_UA}),
        ("met.no", "secondary worldwide weather model", False, "",
         "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=51.5&lon=-0.12",
         {"User-Agent": "polypilot-mcp/1.0 (github.com/xnetcat/polypilot)"}),
        ("espn", "sports scores / game status", False, "",
         "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", None),
        ("thesportsdb", "keyless sports + esports secondary", False, "",
         "https://www.thesportsdb.com/api/v1/json/3/searchevents.php?e=Arsenal_vs_Chelsea", None),
        ("rss", "free news headlines (no key needed)", False, "",
         "https://news.google.com/rss", None),
        ("fred", "official economic data (get_economic_data)", True,
         config.FRED_API_KEY,
         f"https://api.stlouisfed.org/fred/series/observations?series_id=CPIAUCSL"
         f"&api_key={config.FRED_API_KEY}&file_type=json&limit=1", None),
        ("bls", "keyless economic secondary (CPI/unemployment/payrolls)", False, "",
         "https://api.bls.gov/publicAPI/v1/timeseries/data/CUSR0000SA0", None),
        ("pandascore", "esports match data (optional; TheSportsDB covers it keyless)",
         True, config.PANDASCORE_KEY,
         "https://api.pandascore.co/csgo/matches?per_page=1",
         {"Authorization": f"Bearer {config.PANDASCORE_KEY}"}),
        ("newsapi", "broader news coverage (optional; RSS works without it)", True,
         config.NEWSAPI_KEY,
         f"https://newsapi.org/v2/top-headlines?country=us&pageSize=1"
         f"&apiKey={config.NEWSAPI_KEY}", None),
    ]

    for name, note, requires_key, key_value, url, headers in specs:
        key_configured = bool(key_value) if requires_key else None
        entry: dict = {
            "name": name,
            "note": note,
            "requires_key": requires_key,
            "key_configured": key_configured,
        }
        # Probe only when allowed AND (no key needed OR a key is configured).
        if probe and (not requires_key or key_configured):
            entry.update(_probe(url, headers))
        else:
            entry["reachable"] = None
            entry["status"] = "not probed" if probe else "probe disabled"
        sources.append(entry)

    configured = sum(1 for s in sources if s["requires_key"] and s["key_configured"])
    reachable = sum(1 for s in sources if s.get("reachable") is True)
    return {
        "mode": config.MODE,
        "probed": probe,
        "sources": sources,
        "summary": {
            "total": len(sources),
            "keyed_configured": configured,
            "reachable": reachable,
        },
    }
