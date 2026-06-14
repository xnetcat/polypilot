# tests/test_pacing_extra.py
"""Extra breadth for the loop-pacing helper: the accumulation arithmetic of
wait_for_next_cycle (no overshoot, immediate return at zero) and the real
_has_near_resolution book-midpoint logic (mocking only its I/O dependencies)."""
import asyncio
import importlib

import polymarket_mcp.pacing as pacing


def _recorder(slept):
    async def _s(seconds):
        slept.append(seconds)
    return _s


def test_min_seconds_zero_returns_immediately(monkeypatch):
    """min_seconds=0 must not enter the loop at all: zero wait, no early exit."""
    importlib.reload(pacing)
    slept = []
    monkeypatch.setattr(pacing, "_async_sleep", _recorder(slept))
    # _has_near_resolution should never be consulted when there is no time to wait.
    monkeypatch.setattr(pacing, "_has_near_resolution",
                        lambda mode: (_ for _ in ()).throw(
                            AssertionError("should not poll when min_seconds=0")))
    out = asyncio.run(pacing.wait_for_next_cycle(min_seconds=0, mode="paper"))
    assert out["waited_seconds"] == 0
    assert out["early_exit"] is False
    assert slept == []  # never slept


def test_non_divisible_accumulation_never_overshoots(monkeypatch):
    """min_seconds=100 with poll_seconds=30 must total EXACTLY 100, not 120.
    The final chunk is clamped to the remainder (min(poll, remaining))."""
    importlib.reload(pacing)
    slept = []
    monkeypatch.setattr(pacing, "_async_sleep", _recorder(slept))
    monkeypatch.setattr(pacing, "_has_near_resolution", lambda mode: False)
    out = asyncio.run(
        pacing.wait_for_next_cycle(min_seconds=100, mode="paper", poll_seconds=30))
    assert out["waited_seconds"] == 100  # exact, never overshoots
    assert out["early_exit"] is False
    # 30 + 30 + 30 + 10 == 100; the last chunk is clamped to the remainder.
    assert slept == [30, 30, 30, 10]
    assert sum(slept) == 100


def test_has_near_resolution_true_when_midpoint_near_one(monkeypatch):
    """A held position whose book midpoint is ~0.98 (>= 0.95) counts as near
    resolution. Mock the I/O (positions + order book), not the function itself."""
    importlib.reload(pacing)
    monkeypatch.setattr(pacing.state, "get_positions",
                        lambda mode, status="open": [{"token_id": "tok"}])
    monkeypatch.setattr(pacing.order_book, "fetch_order_book",
                        lambda tid: {"best_bid": 0.97, "best_ask": 0.99})
    assert pacing._has_near_resolution("paper") is True


def test_has_near_resolution_false_when_midpoint_midrange(monkeypatch):
    """A midpoint of ~0.50 is nowhere near resolution -> False."""
    importlib.reload(pacing)
    monkeypatch.setattr(pacing.state, "get_positions",
                        lambda mode, status="open": [{"token_id": "tok"}])
    monkeypatch.setattr(pacing.order_book, "fetch_order_book",
                        lambda tid: {"best_bid": 0.49, "best_ask": 0.51})
    assert pacing._has_near_resolution("paper") is False


def test_has_near_resolution_false_when_no_open_positions(monkeypatch):
    """With no open positions the loop body never runs -> False (and the order
    book is never fetched)."""
    importlib.reload(pacing)
    monkeypatch.setattr(pacing.state, "get_positions",
                        lambda mode, status="open": [])
    monkeypatch.setattr(pacing.order_book, "fetch_order_book",
                        lambda tid: (_ for _ in ()).throw(
                            AssertionError("should not fetch book with no positions")))
    assert pacing._has_near_resolution("paper") is False
