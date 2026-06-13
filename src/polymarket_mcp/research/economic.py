"""Economic research — FRED-backed official macro data for economy/finance markets.

Ported from polydegen/research.py (macro helpers), stripping prompt-text
formatting and cache coupling. The FRED API key is read from config.FRED_API_KEY.
"""
from __future__ import annotations

import logging
import re

import httpx

from .. import config

log = logging.getLogger(__name__)

# Simple in-process cache keyed by question text.
_CACHE: dict[str, dict | None] = {}

_FRED_SERIES = [
    {
        "series_id": "CPIAUCSL",
        "label": "Consumer Price Index",
        "keywords": ["consumer price index", "inflation", "cpi"],
    },
    {
        "series_id": "UNRATE",
        "label": "Unemployment Rate",
        "keywords": ["unemployment", "jobless rate"],
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


def _get_macro_research(question: str) -> dict | None:
    if question in _CACHE:
        return _CACHE[question]
    if not config.FRED_API_KEY:
        return None
    series = _detect_macro_series(question)
    if not series:
        return None

    try:
        response = httpx.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": series["series_id"],
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
        if not values:
            return None

        target_value = _extract_numeric_target(question)
        latest_date, latest_value = values[0]
        previous_date = ""
        previous_value = None
        if len(values) > 1:
            previous_date, previous_value = values[1]

        result: dict = {
            "series_id": series["series_id"],
            "series_label": series["label"],
            "latest_date": latest_date,
            "latest_value": latest_value,
            "previous_date": previous_date,
            "previous_value": previous_value,
            "delta_value": latest_value - previous_value if previous_value is not None else None,
            "source": "fred",
            "source_url": f"https://fred.stlouisfed.org/series/{series['series_id']}",
        }
        if target_value is not None and target_value != 0:
            result["target_value"] = target_value
            result["diff_pct"] = round(((latest_value - target_value) / target_value) * 100, 3)
            question_lower = question.lower()
            if "above" in question_lower or "over" in question_lower:
                result["threshold_direction"] = "above"
            elif "below" in question_lower or "under" in question_lower:
                result["threshold_direction"] = "below"
        _CACHE[question] = result
        return result
    except Exception as exc:
        log.debug("[research] FRED fetch error for %s: %s", series["series_id"], exc)
        return None


def fetch_economic_data(question: str) -> dict:
    """FRED-backed official economic data (inflation, GDP, unemployment, rates)
    matched to the question.

    Returns {series_id, series_name, latest_value, latest_date, previous_value,
    target?, source} or an {"error": ...} dict (e.g. when FRED_API_KEY is unset
    or no series matches).
    """
    if not config.FRED_API_KEY:
        return {"error": "FRED_API_KEY not set"}
    research = _get_macro_research(question)
    if not research:
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
    }
    if "target_value" in research:
        result["target"] = {
            "value": research["target_value"],
            "diff_pct": research.get("diff_pct"),
            "direction": research.get("threshold_direction"),
        }
    return result
