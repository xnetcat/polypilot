"""Economic research — FRED-backed official macro data for economy/finance markets.

Ported from polydegen/research.py (macro helpers), stripping prompt-text
formatting and cache coupling. The FRED API key is read from config.FRED_API_KEY.

FRED is the primary source. For the three series FRED republishes from the U.S.
Bureau of Labor Statistics (CPI, unemployment, payrolls) we also pull the
original observation from the free, keyless BLS v1 API as an independent
cross-source confidence check — the same multi-source pattern crypto/sports/
weather use. The two should agree near-exactly (FRED republishes BLS verbatim),
so a divergence flags a stale/mismatched quote. The result carries a per-source
breakdown plus ``value_divergence_pct`` / ``sources_agree``. The BLS pull is
best-effort and never breaks the FRED path.
"""
from __future__ import annotations

import logging
import re

import httpx

from .. import config
from ._cache import _MISS, DEFAULT_TTL_SECONDS, cache_get, cache_set
from ._concurrent import gather

log = logging.getLogger(__name__)

# Simple in-process cache keyed by question text, with a time-based TTL so a
# long-running loop re-pulls fresh FRED observations (see ._cache).
CACHE_TTL_SECONDS: float = DEFAULT_TTL_SECONDS
_CACHE: dict[str, tuple[float, dict | None]] = {}

# FRED is primary; the three series FRED republishes from BLS carry a
# ``bls_series_id`` so we can pull the original BLS observation as an independent
# cross-source confidence check (the same multi-source pattern crypto/sports/
# weather use). FRED republishes BLS verbatim, so the two should agree near-
# exactly; a divergence flags a stale/mismatched quote. The remaining five series
# (FEDFUNDS/GDP/SP500/VIXCLS/DGS10) have no BLS equivalent and stay single-source.
_FRED_SERIES = [
    {
        "series_id": "CPIAUCSL",
        "label": "Consumer Price Index",
        "keywords": ["consumer price index", "inflation", "cpi"],
        "bls_series_id": "CUSR0000SA0",
    },
    {
        "series_id": "UNRATE",
        "label": "Unemployment Rate",
        "keywords": ["unemployment", "jobless rate"],
        "bls_series_id": "LNS14000000",
    },
    {
        "series_id": "FEDFUNDS",
        "label": "Effective Federal Funds Rate",
        "keywords": ["federal funds", "fed funds", "fed rate", "interest rates",
                     "rate cuts", "rate hike", "rates"],
    },
    {
        "series_id": "GDP",
        "label": "Gross Domestic Product",
        "keywords": ["gross domestic product", "gdp"],
    },
    {
        "series_id": "PAYEMS",
        "label": "Nonfarm Payrolls",
        "keywords": ["nonfarm payrolls", "payrolls", "jobs report"],
        "bls_series_id": "CES0000000001",
    },
    {
        "series_id": "SP500",
        "label": "S&P 500 Index",
        "keywords": ["s&p 500", "sp500", "s&p"],
    },
    {
        "series_id": "VIXCLS",
        "label": "VIX Volatility Index",
        "keywords": ["vix", "volatility index"],
    },
    {
        "series_id": "DGS10",
        "label": "10-Year Treasury Yield",
        "keywords": ["10-year treasury", "10 year treasury", "10-year yield",
                     "10 year yield", "10y yield"],
    },
]


def _detect_macro_series(question: str) -> dict | None:
    question_lower = question.lower()
    matches: list[tuple[int, dict]] = []
    for series in _FRED_SERIES:
        best_match = max(
            (len(keyword) for keyword in series["keywords"] if keyword in question_lower),
            default=0,
        )
        if best_match:
            matches.append((best_match, series))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1]


def _extract_numeric_target(question: str) -> float | None:
    for match in re.finditer(r"\$?(-?\d+(?:,\d{3})*(?:\.\d+)?)", question):
        value = match.group(1).replace(",", "")
        try:
            numeric = float(value)
        except ValueError:
            continue
        if abs(numeric) >= 1:
            return numeric
    return None


def _fetch_fred(series_id: str) -> list[tuple[str, float]] | None:
    """Fetch the two latest FRED observations for ``series_id``, newest first.

    Returns ``[(date, value), ...]`` (length 1 or 2) or ``None`` on any error,
    an empty/missing series, or no parseable values. Requires
    ``config.FRED_API_KEY``. Never raises — callers treat ``None`` as "no FRED".
    """
    if not config.FRED_API_KEY:
        return None
    try:
        response = httpx.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": series_id,
                "api_key": config.FRED_API_KEY,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 2,
            },
            timeout=10,
        )
        response.raise_for_status()
        observations = response.json().get("observations", [])
        values: list[tuple[str, float]] = []
        for observation in observations:
            raw_value = str(observation.get("value", "")).strip()
            if not raw_value or raw_value == ".":
                continue
            try:
                values.append((str(observation.get("date", "")), float(raw_value)))
            except ValueError:
                continue
        return values or None
    except Exception as exc:  # noqa: BLE001 — best-effort, never raise.
        log.debug("[research] FRED fetch error for %s: %s", series_id, exc)
        return None


def _fetch_bls(bls_series_id: str) -> dict | None:
    """Fetch the latest observation for ``bls_series_id`` from the BLS v1 API.

    Uses the free, keyless ``timeseries/data`` endpoint. ``data[0]`` is the
    latest observation; ``value`` is a string that we parse to float. The date is
    rendered ``"{year}-{period}"`` (e.g. ``"2026-M05"``). Returns
    ``{"latest_value": float, "latest_date": str}`` or ``None`` on any error,
    a non-success status, or an empty/unparseable series. Never raises — this is
    a cross-source confidence check, so a BLS failure must not break FRED.
    """
    try:
        response = httpx.get(
            f"https://api.bls.gov/publicAPI/v1/timeseries/data/{bls_series_id}",
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "REQUEST_SUCCEEDED":
            return None
        series = payload.get("Results", {}).get("series", [])
        if not series:
            return None
        data = series[0].get("data", [])
        if not data:
            return None
        latest = data[0]
        raw_value = str(latest.get("value", "")).strip()
        if not raw_value:
            return None
        latest_value = float(raw_value)
        latest_date = f"{latest.get('year', '')}-{latest.get('period', '')}"
        return {"latest_value": latest_value, "latest_date": latest_date}
    except Exception as exc:  # noqa: BLE001 — best-effort, never raise.
        log.debug("[research] BLS fetch error for %s: %s", bls_series_id, exc)
        return None


def _get_macro_research(question: str) -> dict | None:
    cached = cache_get(_CACHE, question, CACHE_TTL_SECONDS)
    if cached is not _MISS:
        return cached
    series = _detect_macro_series(question)
    if not series:
        return None

    bls_series_id = series.get("bls_series_id")

    # Fetch FRED (primary) and — when the series has a BLS equivalent — BLS
    # concurrently for cross-source confidence. gather() runs them in parallel
    # threads and never raises (a failing/timed-out source contributes None).
    if bls_series_id:
        fred_values, bls = gather(
            lambda: _fetch_fred(series["series_id"]),
            lambda: _fetch_bls(bls_series_id),
        )
    else:
        fred_values = _fetch_fred(series["series_id"])
        bls = None

    # Top-level latest value/date come from FRED when present. If FRED is
    # unavailable but BLS resolved (e.g. FRED_API_KEY unset), fall back to BLS so
    # the tool still returns data for the three BLS-backed series.
    target_value = _extract_numeric_target(question)
    previous_date = ""
    previous_value = None
    if fred_values:
        latest_date, latest_value = fred_values[0]
        if len(fred_values) > 1:
            previous_date, previous_value = fred_values[1]
        source = "fred"
        source_url = f"https://fred.stlouisfed.org/series/{series['series_id']}"
    elif bls is not None:
        latest_date = bls["latest_date"]
        latest_value = bls["latest_value"]
        source = "bls"
        source_url = f"https://data.bls.gov/timeseries/{bls_series_id}"
    else:
        # Neither source returned data -> preserve the original "no data" result.
        return None

    result: dict = {
        "series_id": series["series_id"],
        "series_label": series["label"],
        "latest_date": latest_date,
        "latest_value": latest_value,
        "previous_date": previous_date,
        "previous_value": previous_value,
        "delta_value": latest_value - previous_value if previous_value is not None else None,
        "source": source,
        "source_url": source_url,
    }

    # Per-source breakdown for cross-source confidence. The ``bls`` key is present
    # ONLY when the series has a BLS equivalent; an unavailable source is marked
    # with an explicit error rather than being silently dropped.
    fred_latest = fred_values[0] if fred_values else None
    sources: dict = {
        "fred": {
            "latest_value": fred_latest[1],
            "latest_date": fred_latest[0],
        } if fred_latest is not None else {"error": "unavailable"},
    }
    if bls_series_id:
        sources["bls"] = (
            {"latest_value": bls["latest_value"], "latest_date": bls["latest_date"]}
            if bls is not None
            else {"error": "unavailable"}
        )
    result["sources"] = sources

    # Relative gap between the FRED and BLS latest values (percent of the FRED
    # value); None unless both are present and FRED is non-zero. ``sources_agree``
    # is True when the divergence is small (<= ~1%), None when fewer than two
    # sources resolved. FRED republishes BLS, so agreement should be near-exact.
    value_divergence_pct = None
    sources_agree = None
    if fred_latest is not None and bls is not None:
        fred_value = fred_latest[1]
        bls_value = bls["latest_value"]
        if fred_value != 0:
            value_divergence_pct = round(
                abs(fred_value - bls_value) / abs(fred_value) * 100, 3
            )
            sources_agree = value_divergence_pct <= 1.0
    result["value_divergence_pct"] = value_divergence_pct
    result["sources_agree"] = sources_agree

    if target_value is not None and target_value != 0:
        result["target_value"] = target_value
        result["diff_pct"] = round(((latest_value - target_value) / target_value) * 100, 3)
        question_lower = question.lower()
        if "above" in question_lower or "over" in question_lower:
            result["threshold_direction"] = "above"
        elif "below" in question_lower or "under" in question_lower:
            result["threshold_direction"] = "below"
    cache_set(_CACHE, question, result)
    return result


def fetch_economic_data(question: str) -> dict:
    """FRED-backed official economic data (inflation, GDP, unemployment, rates)
    matched to the question, with a BLS cross-source confidence check.

    Returns {series_id, series_name, latest_value, latest_date, previous_value,
    target?, source, sources, value_divergence_pct, sources_agree} or an
    {"error": ...} dict (e.g. when no series matches). ``sources`` carries each
    provider's {latest_value, latest_date} (or {"error": "unavailable"}); the
    ``bls`` key is present only for the three series FRED republishes from BLS
    (CPI, unemployment, payrolls). ``value_divergence_pct`` is the relative gap
    between the FRED and BLS latest values (None unless both present), and
    ``sources_agree`` is True when that gap is small (None with fewer than two
    sources).

    FRED requires ``config.FRED_API_KEY``; for the three BLS-backed series the
    keyless BLS API serves as a fallback, so those still resolve without a key.
    """
    research = _get_macro_research(question)
    if not research:
        if not config.FRED_API_KEY:
            return {"error": "FRED_API_KEY not set"}
        return {"error": "no matching FRED series for the question"}
    result: dict = {
        "series_id": research["series_id"],
        "series_name": research["series_label"],
        "latest_value": research["latest_value"],
        "latest_date": research["latest_date"],
        "previous_value": research.get("previous_value"),
        "previous_date": research.get("previous_date"),
        "delta_value": research.get("delta_value"),
        "source": research.get("source", "fred"),
        "source_url": research.get("source_url"),
        "sources": research.get("sources"),
        "value_divergence_pct": research.get("value_divergence_pct"),
        "sources_agree": research.get("sources_agree"),
    }
    if "target_value" in research:
        result["target"] = {
            "value": research["target_value"],
            "diff_pct": research.get("diff_pct"),
            "direction": research.get("threshold_direction"),
        }
    return result
