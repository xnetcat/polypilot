"""Crypto research — CoinGecko spot/24h data for crypto markets.

Ported from polydegen/research.py (crypto helpers), stripping prompt-text
formatting and config/cache coupling.

In addition to spot/24h data, this module estimates an *independent*
probability that the asset lands in the band/threshold described by the
question (e.g. "Will BTC be between $64,000 and $64,500 on June 20?"). The
estimate is a simple lognormal random-walk model:

    sigma_T = daily_sigma * sqrt(horizon_days)
    z(x)    = (ln(x) - ln(spot)) / sigma_T
    Phi(z)  = 0.5 * (1 + erf(z / sqrt(2)))

    band  -> P = Phi(z(high)) - Phi(z(low))
    above -> P = 1 - Phi(z(threshold))
    below -> P =     Phi(z(threshold))

``daily_sigma`` is the stdev of daily log-returns over a recent CoinGecko
``market_chart`` window. The whole probability path is best-effort: any
failure (no series, too few points, unparseable band, zero spot) simply omits
the ``probability`` block — it never raises.
"""
from __future__ import annotations

import datetime
import logging
import math
import re

import httpx

from ._cache import _MISS, DEFAULT_TTL_SECONDS, cache_get, cache_set

log = logging.getLogger(__name__)

# Simple in-process cache keyed by question text, with a time-based TTL so a
# long-running loop re-pulls fresh spot prices (see ._cache).
CACHE_TTL_SECONDS: float = DEFAULT_TTL_SECONDS
_CACHE: dict[str, tuple[float, dict | None]] = {}

# Default forecast horizon (days) when the question names no target date.
DEFAULT_HORIZON_DAYS = 7

# Number of daily samples requested from CoinGecko for the volatility estimate.
_VOL_WINDOW_DAYS = 30

_COINS = {
    "bitcoin": "bitcoin",
    "btc": "bitcoin",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "solana": "solana",
    "sol": "solana",
}

# Month names -> month number, for parsing "on June 20" style target dates.
_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# Weekday names -> Python weekday() index (Mon=0 .. Sun=6).
_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def _parse_money(token: str) -> float | None:
    """Parse a price token like ``$64,000`` or ``64k`` into a float.

    Strips ``$`` and thousands commas, and expands a trailing ``k``/``m``
    suffix (case-insensitive). Returns ``None`` if nothing numeric is found.
    """
    if token is None:
        return None
    t = token.strip().lower().replace(",", "").replace("$", "").strip()
    mult = 1.0
    if t.endswith("k"):
        mult = 1_000.0
        t = t[:-1]
    elif t.endswith("m"):
        mult = 1_000_000.0
        t = t[:-1]
    m = re.search(r"\d+(?:\.\d+)?", t)
    if not m:
        return None
    try:
        return float(m.group(0)) * mult
    except ValueError:
        return None


def _parse_band(question: str) -> dict | None:
    """Parse a band or threshold target from the question.

    Returns one of::

        {"kind": "band",  "low": <float>, "high": <float>}
        {"kind": "above", "threshold": <float>}
        {"kind": "below", "threshold": <float>}

    or ``None`` if no usable price is found. Prices below 100 are ignored to
    avoid matching incidental numbers (dates, percentages).
    """
    q = question.lower()

    # "between $X and $Y" (also "from X to Y").
    band = re.search(
        r"(?:between|from)\s+\$?([\d.,]+\s*[km]?)\s+(?:and|to|-|–)\s+\$?([\d.,]+\s*[km]?)",
        q,
    )
    if band:
        low = _parse_money(band.group(1))
        high = _parse_money(band.group(2))
        if low is not None and high is not None and low > 0 and high > 0:
            if low > high:
                low, high = high, low
            if high > 100:
                return {"kind": "band", "low": low, "high": high}

    # Bare "$X - $Y" / "$X to $Y" range without a leading keyword.
    bare = re.search(
        r"\$([\d.,]+\s*[km]?)\s*(?:-|–|to)\s*\$([\d.,]+\s*[km]?)",
        q,
    )
    if bare:
        low = _parse_money(bare.group(1))
        high = _parse_money(bare.group(2))
        if low is not None and high is not None and low > 0 and high > 0:
            if low > high:
                low, high = high, low
            if high > 100:
                return {"kind": "band", "low": low, "high": high}

    # Single threshold: pick the first $-amount > 100 in the question.
    threshold = None
    for tok in re.findall(r"\$?[\d.,]+\s*[km]?", q):
        val = _parse_money(tok)
        if val is not None and val > 100:
            threshold = val
            break
    if threshold is None:
        return None

    if "below" in q or "under" in q or "less than" in q or "lower than" in q:
        return {"kind": "below", "threshold": threshold}
    if "above" in q or "over" in q or "greater than" in q or "higher than" in q or "at least" in q:
        return {"kind": "above", "threshold": threshold}
    # Default an unqualified threshold to "above" (the common Polymarket framing).
    return {"kind": "above", "threshold": threshold}


def _parse_horizon_days(question: str, today: datetime.date) -> tuple[int, str | None]:
    """Parse a target date from the question and return (days_from_today, note).

    Recognises "on June 20" / "by Friday" / "June 20, 2026" style dates. Falls
    back to ``DEFAULT_HORIZON_DAYS`` with an explanatory note when no date is
    found. The horizon is clamped to a minimum of 1 day so sigma_T > 0.
    """
    q = question.lower()

    # "June 20" / "on June 20" / "June 20, 2026"
    m = re.search(
        r"\b(" + "|".join(_MONTHS) + r")\.?\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?",
        q,
    )
    if m:
        month = _MONTHS[m.group(1)]
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            target = datetime.date(year, month, day)
            # If no explicit year and the date already passed, roll to next year.
            if not m.group(3) and target < today:
                target = datetime.date(year + 1, month, day)
            delta = (target - today).days
            if delta >= 1:
                return delta, None
        except ValueError:
            pass

    # "by Friday" / "on Monday" -> next occurrence of that weekday.
    w = re.search(r"\b(" + "|".join(_WEEKDAYS) + r")\b", q)
    if w:
        target_wd = _WEEKDAYS[w.group(1)]
        delta = (target_wd - today.weekday()) % 7
        if delta == 0:
            delta = 7  # "this Friday" when today *is* Friday -> next Friday.
        return delta, None

    return DEFAULT_HORIZON_DAYS, (
        f"no target date parsed; assumed {DEFAULT_HORIZON_DAYS}-day horizon"
    )


def _fetch_daily_prices(coin_id: str) -> list[float]:
    """Fetch a recent daily close series from CoinGecko ``market_chart``.

    Returns the list of USD prices (``prices[][1]``). Returns ``[]`` on any
    error — callers treat an empty/short series as "no volatility estimate".
    """
    try:
        resp = httpx.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
            params={
                "vs_currency": "usd",
                "days": _VOL_WINDOW_DAYS,
                "interval": "daily",
            },
            timeout=10,
        )
        resp.raise_for_status()
        prices = resp.json().get("prices", []) or []
        out: list[float] = []
        for point in prices:
            try:
                out.append(float(point[1]))
            except (TypeError, ValueError, IndexError):
                continue
        return out
    except Exception as e:  # noqa: BLE001 — best-effort, never raise.
        log.debug("[research] Crypto market_chart fetch error: %s", e)
        return []


def _daily_log_return_sigma(prices: list[float]) -> float | None:
    """Stdev of daily log-returns for ``prices``. ``None`` if < 2 returns."""
    rets: list[float] = []
    prev = None
    for p in prices:
        if prev is not None and prev > 0 and p > 0:
            rets.append(math.log(p / prev))
        prev = p
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)  # sample variance
    sigma = math.sqrt(var)
    if not math.isfinite(sigma) or sigma <= 0:
        return None
    return sigma


def _phi(z: float) -> float:
    """Standard normal CDF via the error function (no scipy)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _estimate_probability(
    band: dict,
    spot: float,
    daily_sigma: float,
    horizon_days: int,
    note: str | None,
) -> dict | None:
    """Lognormal probability that ``spot`` lands in the band/threshold.

    Returns a ``probability`` block, or ``None`` if inputs are unusable.
    """
    if spot <= 0 or daily_sigma <= 0 or horizon_days < 1:
        return None
    sigma_t = daily_sigma * math.sqrt(horizon_days)
    if sigma_t <= 0 or not math.isfinite(sigma_t):
        return None
    ln_spot = math.log(spot)

    def z(x: float) -> float:
        return (math.log(x) - ln_spot) / sigma_t

    result: dict = {
        "daily_vol": round(daily_sigma, 6),
        "horizon_days": horizon_days,
        "model": "lognormal",
    }
    if note:
        result["note"] = note

    kind = band["kind"]
    if kind == "band":
        low, high = band["low"], band["high"]
        if low <= 0 or high <= 0:
            return None
        estimate = _phi(z(high)) - _phi(z(low))
        result.update({"kind": "band", "low": low, "high": high})
    elif kind == "above":
        x = band["threshold"]
        if x <= 0:
            return None
        estimate = 1.0 - _phi(z(x))
        result.update({"kind": "above", "threshold": x})
    elif kind == "below":
        x = band["threshold"]
        if x <= 0:
            return None
        estimate = _phi(z(x))
        result.update({"kind": "below", "threshold": x})
    else:
        return None

    # Clamp to [0, 1] to absorb any tiny floating-point excursions.
    estimate = max(0.0, min(1.0, estimate))
    result["estimate"] = round(estimate, 6)
    return result


def _get_crypto_research(question: str) -> dict | None:
    cached = cache_get(_CACHE, question, CACHE_TTL_SECONDS)
    if cached is not _MISS:
        return cached
    q = question.lower()
    coin_id = None
    matched_keyword = ""
    for keyword, cg_id in _COINS.items():
        if keyword in q:
            coin_id = cg_id
            matched_keyword = keyword
            break
    if not coin_id:
        return None

    try:
        resp = httpx.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": coin_id,
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_24hr_vol": "true",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get(coin_id, {})
        price = float(data.get("usd", 0) or 0)
        change = float(data.get("usd_24h_change", 0) or 0)
        vol = float(data.get("usd_24h_vol", 0) or 0)
        result: dict = {
            "coin_id": coin_id,
            "symbol_hint": matched_keyword,
            "price_usd": price,
            "change_24h_pct": change,
            "volume_24h_usd": vol,
            "source": "coingecko",
            "source_url": f"https://www.coingecko.com/en/coins/{coin_id}",
        }
        price_match = re.search(r"\$?([\d,]+)", question.replace(",", ""))
        if price_match:
            target = float(price_match.group(1).replace(",", ""))
            if target > 100:
                result["target_price"] = target
                result["diff_pct"] = round(((price - target) / target) * 100, 3)
                q_lower = question.lower()
                if "above" in q_lower or "over" in q_lower:
                    result["threshold_direction"] = "above"
                elif "below" in q_lower or "under" in q_lower:
                    result["threshold_direction"] = "below"
        if "up or down" in q:
            result["market_type"] = "up_or_down"
        elif "above" in q or "below" in q or "over" in q or "under" in q:
            result["market_type"] = "threshold"
        else:
            result["market_type"] = "other"
        cache_set(_CACHE, question, result)
        return result
    except Exception as e:
        log.debug("[research] Crypto fetch error: %s", e)
        return None


def _build_probability(question: str, coin_id: str, spot: float) -> dict | None:
    """Best-effort lognormal probability block for the question's band/threshold.

    Returns the ``probability`` dict or ``None`` (never raises). ``None`` means
    the band was unparseable, the series too short, or spot/vol unusable.
    """
    try:
        band = _parse_band(question)
        if band is None:
            return None
        if spot <= 0:
            return None
        prices = _fetch_daily_prices(coin_id)
        daily_sigma = _daily_log_return_sigma(prices)
        if daily_sigma is None:
            return None
        horizon_days, note = _parse_horizon_days(question, datetime.date.today())
        return _estimate_probability(band, spot, daily_sigma, horizon_days, note)
    except Exception as e:  # noqa: BLE001 — probability is strictly best-effort.
        log.debug("[research] Crypto probability error: %s", e)
        return None


def fetch_crypto(question: str) -> dict:
    """Crypto spot price and 24h change/volume. Parses the asset from the question.

    Returns {symbol, coin_id, spot_usd, change_24h, volume_24h, target?,
    probability?, source} or an {"error": ...} dict. The ``probability`` block
    is an independent lognormal estimate of the band/threshold landing
    probability and is omitted when it can't be computed (best-effort).
    """
    research = _get_crypto_research(question)
    if not research:
        return {"error": "no crypto asset detected in the question"}

    symbol_map = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL"}
    coin_id = research["coin_id"]
    spot = research["price_usd"]
    result: dict = {
        "symbol": symbol_map.get(coin_id, coin_id),
        "coin_id": coin_id,
        "spot_usd": spot,
        "change_24h": research["change_24h_pct"],
        "volume_24h": research["volume_24h_usd"],
        "market_type": research.get("market_type"),
        "source": research.get("source", "coingecko"),
        "source_url": research.get("source_url"),
    }
    if "target_price" in research:
        result["target"] = {
            "price": research["target_price"],
            "diff_pct": research.get("diff_pct"),
            "direction": research.get("threshold_direction"),
        }

    probability = _build_probability(question, coin_id, spot)
    if probability is not None:
        result["probability"] = probability

    return result
