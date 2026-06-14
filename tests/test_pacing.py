# tests/test_pacing.py
import asyncio
import importlib

import pytest

import polymarket_mcp.pacing as pacing


def _recorder(slept):
    async def _s(seconds):
        slept.append(seconds)
    return _s


def test_waits_full_interval_when_quiet(monkeypatch):
    importlib.reload(pacing)
    slept = []
    monkeypatch.setattr(pacing, "_async_sleep", _recorder(slept))
    monkeypatch.setattr(pacing, "_has_near_resolution", lambda mode: False)
    out = asyncio.run(pacing.wait_for_next_cycle(min_seconds=120, mode="paper"))
    assert out["waited_seconds"] >= 120
    assert out["early_exit"] is False
    assert sum(slept) == 120


def test_early_exit_when_position_near_resolution(monkeypatch):
    importlib.reload(pacing)
    monkeypatch.setattr(pacing, "_async_sleep", _recorder([]))
    calls = {"n": 0}

    def near(mode):
        calls["n"] += 1
        return calls["n"] >= 2

    monkeypatch.setattr(pacing, "_has_near_resolution", near)
    out = asyncio.run(
        pacing.wait_for_next_cycle(min_seconds=600, mode="paper", poll_seconds=30))
    assert out["early_exit"] is True


def test_wait_is_cancellable(monkeypatch):
    # The whole point of the async rewrite: a cancel mid-wait must interrupt
    # promptly (a blocking time.sleep would ignore it and freeze the server).
    importlib.reload(pacing)
    monkeypatch.setattr(pacing, "_has_near_resolution", lambda mode: False)

    async def run():
        task = asyncio.ensure_future(
            pacing.wait_for_next_cycle(min_seconds=600, mode="paper", poll_seconds=1))
        await asyncio.sleep(0.05)   # let it enter the first real asyncio.sleep
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
