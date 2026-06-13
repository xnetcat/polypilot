"""Loop pacing: a server-side wait that can return early when a held position
nears resolution."""
from __future__ import annotations

import time

from . import config, order_book, state


def _has_near_resolution(mode: str) -> bool:
    for pos in state.get_positions(mode, status="open"):
        book = order_book.fetch_order_book(pos["token_id"])
        bid, ask = book.get("best_bid"), book.get("best_ask")
        if bid is None or ask is None:
            continue
        mid = (bid + ask) / 2
        if mid >= 0.95 or mid <= 0.05:
            return True
    return False


def wait_for_next_cycle(min_seconds: int = 300, mode: str | None = None,
                        poll_seconds: int = 30) -> dict:
    """Sleep up to min_seconds, polling every poll_seconds; return early if a held
    position nears resolution."""
    mode = mode or config.MODE
    waited = 0
    while waited < min_seconds:
        chunk = min(poll_seconds, min_seconds - waited)
        time.sleep(chunk)
        waited += chunk
        if _has_near_resolution(mode):
            return {"waited_seconds": waited, "early_exit": True,
                    "reason": "a held position is near resolution"}
    return {"waited_seconds": waited, "early_exit": False}
