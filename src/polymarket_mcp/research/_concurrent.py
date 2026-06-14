"""Run independent, blocking source fetches concurrently (best-effort).

Multi-source research calls several free APIs per market (e.g. CoinGecko AND
Binance, ESPN AND TheSportsDB). Doing them sequentially adds their latencies;
this runs each in its own thread so the wall-clock is the slowest single source.
Each callable's exception (or a timeout) yields ``None`` rather than propagating,
so one slow/failing source never breaks the others or the research call.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable


def gather(*funcs: Callable, timeout: float = 20.0) -> list:
    """Run each zero-arg callable in parallel; return results in submission order.

    A callable that raises or exceeds ``timeout`` contributes ``None``. Bounded
    in practice by each source's own HTTP timeout (~10-15s), so the pool's
    shutdown wait is short. Never raises.
    """
    if not funcs:
        return []
    out: list = [None] * len(funcs)
    with ThreadPoolExecutor(max_workers=min(len(funcs), 8)) as ex:
        futures = [ex.submit(f) for f in funcs]
        for i, fut in enumerate(futures):
            try:
                out[i] = fut.result(timeout=timeout)
            except Exception:
                out[i] = None
    return out
