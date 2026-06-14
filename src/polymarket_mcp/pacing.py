"""Loop pacing: a server-side wait that can return early when a held position
nears resolution.

The wait is ASYNC on purpose. FastMCP runs a sync tool inline on its event loop,
so a blocking ``time.sleep`` here would freeze the whole MCP server for the entire
interval — it could not service a client cancellation or any new tool call until
the sleep returned (a 25-minute wait => a 25-minute freeze, and a cancel mid-wait
would hang every later call). ``asyncio.sleep`` yields control back to the loop
between polls and lets a cancellation actually interrupt the wait, so cancelling
mid-wait frees the server immediately.
"""
from __future__ import annotations

import asyncio

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


async def _async_sleep(seconds: float) -> None:
    """Indirection over ``asyncio.sleep`` so tests can stub the wait without
    touching the global event loop."""
    await asyncio.sleep(seconds)


async def wait_for_next_cycle(min_seconds: int = 300, mode: str | None = None,
                              poll_seconds: int = 30) -> dict:
    """Async, cancellable sleep up to ``min_seconds``, polling every
    ``poll_seconds``; returns early if a held position nears resolution. Being a
    coroutine, FastMCP awaits it — so the server loop stays responsive and a
    client cancellation stops the wait instead of hanging the next call."""
    mode = mode or config.MODE
    waited = 0
    while waited < min_seconds:
        chunk = min(poll_seconds, min_seconds - waited)
        await _async_sleep(chunk)
        waited += chunk
        if _has_near_resolution(mode):
            return {"waited_seconds": waited, "early_exit": True,
                    "reason": "a held position is near resolution"}
    return {"waited_seconds": waited, "early_exit": False}
