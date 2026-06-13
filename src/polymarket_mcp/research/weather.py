"""Weather research — structured Open-Meteo forecasts for weather markets.

Ported from polydegen/research.py (weather helpers), stripping the prompt-text
formatting and the config/cache coupling. A small in-process dict caches results.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

# Simple in-process caches (no config TTL coupling in the new package).
_CACHE: dict[str, dict | None] = {}
_GEOCODE_CACHE: dict[str, tuple[str, tuple[float, float], bool, str] | None] = {}

# Mapping of city names to coordinates for Open-Meteo.
CITY_COORDS = {
    "tokyo": (35.6762, 139.6503),
    "beijing": (39.9042, 116.4074),
    "shanghai": (31.2304, 121.4737),
    "seoul": (37.5665, 126.9780),
    "london": (51.5074, -0.1278),
    "paris": (48.8566, 2.3522),
    "new york": (40.7128, -74.0060),
    "los angeles": (34.0522, -118.2437),
    "chicago": (41.8781, -87.6298),
    "houston": (29.7604, -95.3698),
    "denver": (39.7392, -104.9903),
    "miami": (25.7617, -80.1918),
    "singapore": (1.3521, 103.8198),
    "taipei": (25.0330, 121.5654),
    "ankara": (39.9334, 32.8597),
    "chongqing": (29.4316, 106.9123),
    "shenzhen": (22.5431, 114.0579),
    "tel aviv": (32.0853, 34.7818),
    "wuhan": (30.5928, 114.3055),
    "mexico city": (19.4326, -99.1332),
    "san francisco": (37.7749, -122.4194),
    "sydney": (-33.8688, 151.2093),
    "mumbai": (19.0760, 72.8777),
    "dubai": (25.2048, 55.2708),
    "moscow": (55.7558, 37.6173),
    "berlin": (52.5200, 13.4050),
    "rome": (41.9028, 12.4964),
    "madrid": (40.4168, -3.7038),
    "lisbon": (38.7223, -9.1393),
    "cairo": (30.0444, 31.2357),
    "nairobi": (-1.2921, 36.8219),
    "bangkok": (13.7563, 100.5018),
    "jakarta": (-6.2088, 106.8456),
    "manila": (14.5995, 120.9842),
    "ho chi minh": (10.8231, 106.6297),
    "kuala lumpur": (3.1390, 101.6869),
}
_US_CITIES = {
    "new york", "los angeles", "chicago", "houston", "denver", "miami",
    "san francisco",
}


def _fahrenheit_to_c(value_f: float) -> float:
    return (value_f - 32.0) * 5.0 / 9.0


def _normalize_city_name(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z .'-]", " ", value.lower())
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip(" ,.-")


def _extract_city(question: str) -> str | None:
    """Extract city name from a market question."""
    q = question.lower()
    for city in sorted(CITY_COORDS.keys(), key=len, reverse=True):
        if city in q:
            return city
    patterns = [
        r"\btemperature in\s+([a-z .'-]+?)(?:\s+be\b|\s+between\b|\s+on\b|\s+by\b|\s+today\b|\s+tomorrow\b|\?|$)",
        r"\bin\s+([a-z .'-]+?)(?:\s+be\b|\s+between\b|\s+on\b|\s+by\b|\s+today\b|\s+tomorrow\b|\?|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, q)
        if not match:
            continue
        candidate = _normalize_city_name(match.group(1))
        if candidate:
            return candidate
    return None


def _extract_date(question: str) -> str | None:
    """Extract date from question like 'April 6' -> '2026-04-06'."""
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    q = question.lower()
    for month_name, month_num in months.items():
        match = re.search(rf"{month_name}\s+(\d{{1,2}})", q)
        if match:
            day = int(match.group(1))
            year = datetime.now().year
            return f"{year}-{month_num:02d}-{day:02d}"
    return None


def _extract_weather_target(question: str) -> dict[str, object]:
    q = question.lower()
    range_patterns = [
        r"between\s*(-?\d+(?:\.\d+)?)\s*(?:-|to|and)\s*(-?\d+(?:\.\d+)?)\s*°?\s*([cf])",
        r"\b(-?\d+(?:\.\d+)?)\s*(?:-|to)\s*(-?\d+(?:\.\d+)?)\s*°?\s*([cf])",
    ]
    for pattern in range_patterns:
        match = re.search(pattern, q)
        if not match:
            continue
        lower = float(match.group(1))
        upper = float(match.group(2))
        unit = match.group(3).upper()
        lower, upper = sorted((lower, upper))
        lower_c = _fahrenheit_to_c(lower) if unit == "F" else lower
        upper_c = _fahrenheit_to_c(upper) if unit == "F" else upper
        return {
            "market_type": "range_temperature",
            "target_unit": unit,
            "target_range_low": lower,
            "target_range_high": upper,
            "target_range_low_c": round(lower_c, 3),
            "target_range_high_c": round(upper_c, 3),
            "target_range_midpoint_c": round((lower_c + upper_c) / 2.0, 3),
            "target_range_width_c": round(upper_c - lower_c, 3),
        }

    # Detect directional threshold markets BEFORE the exact fallback (order
    # matters — "25°C or below" matches the exact regex too, so threshold
    # keywords must be checked first).
    threshold_match = re.search(
        r"(-?\d+(?:\.\d+)?)\s*°?\s*([cf])\s*(or\s+(below|lower|above|higher))?\b", q
    )
    if threshold_match:
        direction_word = (threshold_match.group(4) or "").lower()
        has_above_kw = bool(re.search(r"\b(above|or higher|or above)\b", q))
        has_below_kw = bool(re.search(r"\b(below|or lower|or below)\b", q))
        if direction_word in ("below", "lower") or has_below_kw:
            direction = "below"
        elif direction_word in ("above", "higher") or has_above_kw:
            direction = "above"
        else:
            direction = None
        if direction is not None:
            target_value = float(threshold_match.group(1))
            unit = threshold_match.group(2).upper()
            target_temp_c = _fahrenheit_to_c(target_value) if unit == "F" else target_value
            return {
                "market_type": f"threshold_{direction}",
                "target_unit": unit,
                "target_temp": target_value,
                "target_temp_c": round(target_temp_c, 3),
                "threshold_direction": direction,
            }

    exact_match = re.search(r"(-?\d+(?:\.\d+)?)\s*°?\s*([cf])", q)
    if not exact_match:
        return {}
    target_value = float(exact_match.group(1))
    unit = exact_match.group(2).upper()
    target_temp_c = _fahrenheit_to_c(target_value) if unit == "F" else target_value
    return {
        "market_type": "exact_temperature",
        "target_unit": unit,
        "target_temp": target_value,
        "target_temp_c": round(target_temp_c, 3),
    }


def extract_weather_identity(question: str) -> dict | None:
    """Return {city, date, market_type, target_temp, target_unit} for a weather
    question, or None if it doesn't parse as an exact-temperature market.

    Used by the ladder-sandwich logic to find sibling markets at target±1.
    """
    city = _extract_city(question)
    date = _extract_date(question)
    target = _extract_weather_target(question)
    if not (city and date and target.get("market_type") == "exact_temperature"):
        return None
    return {
        "city": city,
        "date": date,
        "market_type": target.get("market_type"),
        "target_temp": target.get("target_temp"),
        "target_unit": target.get("target_unit"),
    }


def _lookup_city_location(
    city: str,
) -> tuple[str, tuple[float, float], bool, str] | None:
    normalized = _normalize_city_name(city)
    if not normalized:
        return None
    if normalized in CITY_COORDS:
        return normalized, CITY_COORDS[normalized], normalized in _US_CITIES, "catalog"
    if normalized in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[normalized]

    try:
        response = httpx.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "en", "format": "json"},
            timeout=10,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        if not results:
            _GEOCODE_CACHE[normalized] = None
            return None

        result = results[0]
        canonical_city = _normalize_city_name(str(result.get("name") or city))
        if not canonical_city:
            canonical_city = normalized
        coords = (float(result["latitude"]), float(result["longitude"]))
        is_us = str(result.get("country_code", "")).upper() == "US"
        payload = (canonical_city, coords, is_us, "open-meteo-geocoding")
        CITY_COORDS.setdefault(canonical_city, coords)
        _GEOCODE_CACHE[normalized] = payload
        return payload
    except Exception as exc:
        log.debug("[research] Geocoding error for %s: %s", city, exc)
        _GEOCODE_CACHE[normalized] = None
        return None


def _get_weather_research(question: str) -> dict | None:
    if question in _CACHE:
        return _CACHE[question]
    city = _extract_city(question)
    if not city:
        return None

    location = _lookup_city_location(city)
    if not location:
        return None
    city, coords, is_us, location_source = location

    target_date = _extract_date(question)
    target = _extract_weather_target(question)

    try:
        resp = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": coords[0],
                "longitude": coords[1],
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode",
                "timezone": "auto",
                "forecast_days": 7,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        maxs = daily.get("temperature_2m_max", [])
        mins = daily.get("temperature_2m_min", [])
        precip = daily.get("precipitation_sum", [])

        forecast_days = []
        for i, date_value in enumerate(dates):
            forecast_days.append(
                {
                    "date": date_value,
                    "high_c": float(maxs[i]),
                    "low_c": float(mins[i]),
                    "precip_mm": float(precip[i]),
                    "is_target": bool(target_date and target_date == date_value),
                }
            )

        result: dict = {
            "city": city,
            "target_date": target_date,
            "forecast_days": forecast_days,
            "source": "open-meteo",
            "source_url": "https://api.open-meteo.com/v1/forecast",
            "location_source": location_source,
            **target,
        }
        if target_date and target_date in dates:
            idx = dates.index(target_date)
            forecast_high = float(maxs[idx])
            result["forecast_high_c"] = forecast_high
            result["forecast_low_c"] = float(mins[idx])
            result["forecast_precip_mm"] = float(precip[idx])
            if result.get("market_type") == "exact_temperature" and result.get("target_temp_c") is not None:
                result["target_diff_c"] = round(forecast_high - float(result["target_temp_c"]), 3)
            elif result.get("market_type") in ("threshold_above", "threshold_below") and result.get("target_temp_c") is not None:
                diff = round(forecast_high - float(result["target_temp_c"]), 3)
                result["target_diff_c"] = diff
                direction = result.get("threshold_direction")
                result["threshold_yes_supported"] = (
                    (direction == "above" and diff >= 0)
                    or (direction == "below" and diff <= 0)
                )
            elif result.get("market_type") == "range_temperature":
                low_c = result.get("target_range_low_c")
                high_c = result.get("target_range_high_c")
                if low_c is not None and high_c is not None:
                    if float(low_c) <= forecast_high <= float(high_c):
                        result["target_in_range"] = True
                        result["target_range_distance_c"] = 0.0
                    elif forecast_high < float(low_c):
                        result["target_in_range"] = False
                        result["target_range_distance_c"] = round(float(low_c) - forecast_high, 3)
                    else:
                        result["target_in_range"] = False
                        result["target_range_distance_c"] = round(forecast_high - float(high_c), 3)
        _CACHE[question] = result
        return result
    except Exception as e:
        log.debug("[research] Weather fetch error for %s: %s", city, e)
        return None


def fetch_weather_forecast(question: str) -> dict:
    """Structured forecast for a weather market question.

    Returns city, date, parsed target, forecast high/low (°C), and the full
    7-day forecast, or an {"error": ...} dict when the question can't be parsed
    or no forecast is available.
    """
    research = _get_weather_research(question)
    if not research:
        identity = extract_weather_identity(question)
        if identity:
            return {"error": "no forecast available", **identity}
        return {"error": "could not parse a city/date from the question"}
    return {
        "city": research.get("city"),
        "date": research.get("target_date"),
        "target": {
            k: research[k]
            for k in (
                "market_type", "target_temp", "target_temp_c", "target_unit",
                "threshold_direction", "target_range_low_c", "target_range_high_c",
            )
            if k in research
        },
        "forecast_high_c": research.get("forecast_high_c"),
        "forecast_low_c": research.get("forecast_low_c"),
        "forecast_precip_mm": research.get("forecast_precip_mm"),
        "target_diff_c": research.get("target_diff_c"),
        "forecast_days": research.get("forecast_days", []),
        "source": research.get("source", "open-meteo"),
    }
