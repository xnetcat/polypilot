# tests/test_pacing.py
import importlib
import polymarket_mcp.pacing as pacing


def test_waits_full_interval_when_quiet(monkeypatch):
    importlib.reload(pacing)
    slept = []
    monkeypatch.setattr(pacing.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(pacing, "_has_near_resolution", lambda mode: False)
    out = pacing.wait_for_next_cycle(min_seconds=120, mode="paper")
    assert out["waited_seconds"] >= 120
    assert out["early_exit"] is False


def test_early_exit_when_position_near_resolution(monkeypatch):
    importlib.reload(pacing)
    monkeypatch.setattr(pacing.time, "sleep", lambda s: None)
    calls = {"n": 0}
    def near(mode):
        calls["n"] += 1
        return calls["n"] >= 2
    monkeypatch.setattr(pacing, "_has_near_resolution", near)
    out = pacing.wait_for_next_cycle(min_seconds=600, mode="paper", poll_seconds=30)
    assert out["early_exit"] is True
