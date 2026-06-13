"""Order book + price history from the Polymarket CLOB API."""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from . import config


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel] = field(default_factory=list)

    @property
    def bid_volume(self) -> float:
        return sum(l.size for l in self.bids)

    @property
    def ask_volume(self) -> float:
        return sum(l.size for l in self.asks)

    @property
    def imbalance(self) -> float:
        total = self.bid_volume + self.ask_volume
        return 0.0 if total == 0 else (self.bid_volume - self.ask_volume) / total

    @property
    def spread(self) -> float:
        if not self.bids or not self.asks:
            return 0.0
        return self.asks[0].price - self.bids[0].price

    @property
    def midpoint(self) -> float:
        if not self.bids or not self.asks:
            return 0.0
        return (self.bids[0].price + self.asks[0].price) / 2


def _levels(raw: list, descending: bool) -> list[OrderBookLevel]:
    levels = []
    for lvl in raw or []:
        try:
            levels.append(OrderBookLevel(float(lvl["price"]), float(lvl["size"])))
        except (KeyError, TypeError, ValueError):
            continue
    levels.sort(key=lambda l: l.price, reverse=descending)
    return levels


def fetch_order_book(token_id: str) -> dict:
    """Fetch and summarize the live order book for a CLOB token id."""
    try:
        resp = httpx.get(f"{config.POLYMARKET_HOST}/book",
                         params={"token_id": token_id}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": f"order book fetch failed: {e}", "token_id": token_id}
    book = OrderBook(bids=_levels(data.get("bids"), descending=True),
                     asks=_levels(data.get("asks"), descending=False))
    return {
        "token_id": token_id,
        "best_bid": book.bids[0].price if book.bids else None,
        "best_ask": book.asks[0].price if book.asks else None,
        "spread": round(book.spread, 4),
        "midpoint": round(book.midpoint, 4),
        "imbalance": round(book.imbalance, 4),
        "bid_volume": round(book.bid_volume, 2),
        "ask_volume": round(book.ask_volume, 2),
        "bids": [{"price": l.price, "size": l.size} for l in book.bids[:10]],
        "asks": [{"price": l.price, "size": l.size} for l in book.asks[:10]],
    }


def fetch_price_history(token_id: str, interval: str = "1d") -> dict:
    """Fetch CLOB price history for a token. interval one of: 1m,1h,6h,1d,1w,max."""
    try:
        resp = httpx.get(f"{config.POLYMARKET_HOST}/prices-history",
                         params={"market": token_id, "interval": interval, "fidelity": 60},
                         timeout=10)
        resp.raise_for_status()
        points = resp.json().get("history", [])
    except Exception as e:
        return {"error": f"price history fetch failed: {e}", "token_id": token_id}
    prices = [float(p["p"]) for p in points if "p" in p]
    if not prices:
        return {"token_id": token_id, "points": 0}
    return {
        "token_id": token_id,
        "interval": interval,
        "points": len(prices),
        "first": prices[0],
        "last": prices[-1],
        "high": max(prices),
        "low": min(prices),
        "change": round(prices[-1] - prices[0], 4),
    }
