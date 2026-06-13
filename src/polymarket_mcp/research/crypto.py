"""Crypto research — CoinGecko spot/24h data for crypto markets.

Ported from polydegen/research.py (crypto helpers), stripping prompt-text
formatting and config/cache coupling. An optional Chronos prediction is attached
behind a try/except so the `ml` extra stays optional (crypto_model is absent in
this repo, so _maybe_prediction always returns None here — by design).
"""
from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger(__name__)

# Simple in-process cache keyed by question text.
_CACHE: dict[str, dict | None] = {}

_COINS = {
    "bitcoin": "bitcoin",
    "btc": "bitcoin",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "solana": "solana",
    "sol": "solana",
}


def _maybe_prediction(symbol: str) -> dict | None:
    """Attach a Chronos direction prediction if the `ml` extra is installed.

    The crypto_model module does not exist in this package, so the import will
    fail and this returns None — that is expected and correct.
    """
    try:
        from ..crypto_model import predict  # only if ml extra installed
    except Exception:
        return None
    try:
        p = predict(symbol)
        return {"direction": p.direction, "confidence": p.confidence}
    except Exception:
        return None


def _get_crypto_research(question: str) -> dict | None:
    if question in _CACHE:
        return _CACHE[question]
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
        _CACHE[question] = result
        return result
    except Exception as e:
        log.debug("[research] Crypto fetch error: %s", e)
        return None


def fetch_crypto(question: str) -> dict:
    """Crypto spot price, 24h change/volume, and (if the `ml` extra is installed)
    a Chronos direction prediction. Parses the asset from the question.

    Returns {symbol, coin_id, spot_usd, change_24h, volume_24h, target?,
    prediction?, source} or an {"error": ...} dict.
    """
    research = _get_crypto_research(question)
    if not research:
        return {"error": "no crypto asset detected in the question"}

    symbol_map = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL"}
    coin_id = research["coin_id"]
    result: dict = {
        "symbol": symbol_map.get(coin_id, coin_id),
        "coin_id": coin_id,
        "spot_usd": research["price_usd"],
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
    prediction = _maybe_prediction(coin_id)
    if prediction is not None:
        result["prediction"] = prediction
    return result
