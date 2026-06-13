"""Tiny time-based TTL cache shared by the research modules.

The research helpers each keep a module-level ``_CACHE`` dict so a single
trading loop doesn't hammer the upstream APIs (Open-Meteo, CoinGecko, ESPN,
FRED). Without expiry those entries live for the whole process, so a
long-running loop can stale-serve forecasts/prices indefinitely. This module
adds a minimal insertion-time stamp so expired entries are treated as misses
and re-fetched.

Usage (drop-in for a plain ``dict`` keyed by question text)::

    from ._cache import cache_get, cache_set

    hit = cache_get(_CACHE, question)        # None on miss OR expiry
    if hit is not None:
        return hit
    ...
    cache_set(_CACHE, question, result)

Stored values are wrapped as ``(inserted_monotonic, value)`` tuples; callers
only ever see the unwrapped value. ``None`` is a legitimate cached value (the
helpers cache "no result" as ``None``), so callers must distinguish a miss
from a cached ``None`` via the ``default`` sentinel if that matters — the
research modules instead simply re-fetch on a ``None`` hit, which is cheap and
correct.
"""
from __future__ import annotations

import time

# Default time-to-live for cached research results, in seconds. Ten minutes is
# long enough to dedupe the repeated lookups within one analysis pass but short
# enough that a multi-hour trading loop re-pulls fresh data.
DEFAULT_TTL_SECONDS = 600.0

# Sentinel distinguishing "key absent / expired" from a stored ``None`` value.
_MISS = object()


def _now() -> float:
    """Monotonic clock used for TTL stamping.

    Wrapped in a module function so tests can monkeypatch the clock to force
    expiry deterministically (``monkeypatch.setattr(_cache, "_now", ...)``).
    ``time.monotonic`` is used so cache validity is unaffected by wall-clock
    adjustments (NTP steps, DST, etc.).
    """
    return time.monotonic()


def cache_get(
    store: dict,
    key: str,
    ttl: float = DEFAULT_TTL_SECONDS,
    default=_MISS,
):
    """Return the cached value for ``key`` if present and not expired.

    A negative or zero ``ttl`` forces every entry to be treated as expired,
    which is the simplest way for tests to force a re-fetch. Expired entries
    are evicted on access. Returns ``default`` (``_MISS`` by default) when the
    key is absent or expired.
    """
    entry = store.get(key, _MISS)
    if entry is _MISS:
        return default
    inserted, value = entry
    if ttl <= 0 or (_now() - inserted) >= ttl:
        # Expired (or TTL disabled): evict and report a miss.
        store.pop(key, None)
        return default
    return value


def cache_set(store: dict, key: str, value) -> None:
    """Store ``value`` for ``key``, stamping it with the current time."""
    store[key] = (_now(), value)
