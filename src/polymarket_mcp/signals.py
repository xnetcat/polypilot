"""Technical market signals derived from CLOB price history + order book.

Best-effort: every entry point catches its own errors and returns an
``{"error": ...}`` dict rather than raising. Metric formulas are ported from
the polydegen ``market_data.py`` predecessor (imbalance, price_change_pct,
retracement_from_high/pullback, momentum z-score).
"""
from __future__ import annotations

import statistics

import httpx

from . import config, order_book


def _fetch_price_series(token_id: str) -> list[float]:
    """Fetch the raw CLOB price-history series as a float list.

    GET {host}/prices-history?market=<token>&interval=1d&fidelity=60 and parse
    history[].p into floats. Raises on transport/HTTP/parse failure so the
    caller can map it to an error dict.
    """
    resp = httpx.get(
        f"{config.POLYMARKET_HOST}/prices-history",
        params={"market": token_id, "interval": "1d", "fidelity": 60},
        timeout=10,
    )
    resp.raise_for_status()
    history = resp.json().get("history", [])
    return [float(p["p"]) for p in history if "p" in p]


def _momentum(prices: list[float]) -> float:
    """Z-score of the most recent price delta vs the distribution of deltas.

    0 if fewer than 3 points (need >=2 deltas to compute a stdev).
    """
    if len(prices) < 3:
        return 0.0
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    mean = statistics.fmean(deltas)
    stdev = statistics.pstdev(deltas)
    if stdev == 0:
        return 0.0
    return (deltas[-1] - mean) / stdev


def _trend(first: float, last: float) -> str:
    if last > first:
        return "up"
    if last < first:
        return "down"
    return "flat"


def _pullback(prices: list[float]) -> float:
    """Retracement of the current price within the series high/low range (0-1).

    On an uptrend (last >= first) this is the retracement from the high
    (0 = at high, 1 = back at low). On a downtrend it is the bounce from the
    low (0 = at low, 1 = back at high). 0 when high == low.
    """
    high = max(prices)
    low = min(prices)
    if high == low:
        return 0.0
    current = prices[-1]
    if prices[-1] >= prices[0]:
        return (high - current) / (high - low)
    return (current - low) / (high - low)


def _divergence(price_change: float, imbalance: float) -> str:
    """Price vs order-book-flow divergence.

    Price falling but imbalance bid-heavy (positive) = bullish divergence.
    Price rising but imbalance ask-heavy (negative) = bearish divergence.
    """
    if price_change < 0 and imbalance > 0:
        return "bullish"
    if price_change > 0 and imbalance < 0:
        return "bearish"
    return "none"


def market_signals(token_id: str) -> dict:
    """Compute technical signals for a CLOB token id.

    Returns a dict with momentum, trend, price_change, pullback, imbalance,
    divergence, spread, midpoint, points and a one-line summary. On any fetch
    failure returns ``{"error": ...}``; never raises.
    """
    try:
        prices = _fetch_price_series(token_id)
    except Exception as e:
        return {"error": f"price history fetch failed: {e}", "token_id": token_id}

    if not prices:
        return {"error": "no price history", "token_id": token_id}

    book = order_book.fetch_order_book(token_id)
    if "error" in book:
        return {"error": book["error"], "token_id": token_id}

    first, last = prices[0], prices[-1]
    price_change = last - first
    imbalance = book.get("imbalance", 0.0)
    spread = book.get("spread", 0.0)
    midpoint = book.get("midpoint", 0.0)

    momentum = _momentum(prices)
    trend = _trend(first, last)
    pullback = _pullback(prices)
    divergence = _divergence(price_change, imbalance)

    summary = (
        f"{trend} trend, momentum z={momentum:+.2f}, "
        f"price change {price_change:+.4f}, pullback {pullback:.2f}, "
        f"imbalance {imbalance:+.2f}"
    )
    if divergence != "none":
        summary += f", {divergence} divergence"

    return {
        "token_id": token_id,
        "momentum": round(momentum, 4),
        "trend": trend,
        "price_change": round(price_change, 4),
        "pullback": round(pullback, 4),
        "imbalance": round(imbalance, 4),
        "divergence": divergence,
        "spread": round(spread, 4),
        "midpoint": round(midpoint, 4),
        "points": len(prices),
        "summary": summary,
    }
