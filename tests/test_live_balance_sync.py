# tests/test_live_balance_sync.py
"""Anchoring LIVE cash to the real on-chain USDC balance.

  * ``state.set_cash`` overwrites cash directly (and starting_balance only when
    flagged), persists, and returns the updated portfolio dict.
  * ``execution.sync_live_balance`` reads the wallet balance via the (mocked)
    ``get_live_balance`` and pegs local cash to it. On a FRESH live portfolio
    (trade_count 0) the first sync also sets the starting-balance baseline, so
    conservation holds (cash == starting, exposure 0, realized 0). A later sync
    after a trade only updates cash, never the baseline. It never raises: no key
    -> error dict with NO state change; a failing balance read -> graceful
    error/unavailable.

Uses the standard reload fixture so config -> db -> state -> execution pick up
the patched env (and db._INITIALIZED, a module-level cache, is reset).
"""
import importlib

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "200")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_DAILY_USD", "30")
    monkeypatch.setenv("POLYPILOT_KILL_SWITCH_TOTAL_USD", "60")
    import polymarket_mcp.config as cfg
    import polymarket_mcp.db as db
    import polymarket_mcp.state as state
    import polymarket_mcp.risk as risk
    import polymarket_mcp.execution as ex
    for m in (cfg, db, state, risk, ex):
        importlib.reload(m)
    return state, ex


# --------------------------------------------------------------------------- #
# state.set_cash
# --------------------------------------------------------------------------- #
def test_set_cash_updates_cash_only_by_default(env):
    state, _ = env
    # Seeded baseline is the configured starting balance.
    before = state.get_portfolio("live")
    assert before["cash"] == 200.0
    assert before["starting_balance"] == 200.0

    updated = state.set_cash("live", 137.42)
    # Returns the updated portfolio dict.
    assert updated["cash"] == 137.42
    # starting_balance is NOT touched unless flagged.
    assert updated["starting_balance"] == 200.0

    # Change persisted to the DB (fresh read agrees).
    assert state.get_portfolio("live")["cash"] == 137.42
    assert state.get_portfolio("live")["starting_balance"] == 200.0


def test_set_cash_with_set_starting_updates_baseline_too(env):
    state, _ = env
    updated = state.set_cash("live", 88.0, set_starting=True)
    assert updated["cash"] == 88.0
    assert updated["starting_balance"] == 88.0
    # Persisted.
    persisted = state.get_portfolio("live")
    assert persisted["cash"] == 88.0
    assert persisted["starting_balance"] == 88.0


def test_set_cash_creates_portfolio_if_missing(env):
    state, _ = env
    # No get_portfolio called first; set_cash must ensure the row exists.
    updated = state.set_cash("live", 50.0, set_starting=True)
    assert updated["cash"] == 50.0
    assert updated["starting_balance"] == 50.0
    assert updated["realized_pnl"] == 0.0
    assert updated["open_positions"] == 0


def test_set_cash_does_not_touch_paper(env):
    state, _ = env
    # Anchoring LIVE leaves the separate PAPER portfolio untouched.
    state.set_cash("live", 5.0, set_starting=True)
    assert state.get_portfolio("paper")["cash"] == 200.0
    assert state.get_portfolio("live")["cash"] == 5.0


# --------------------------------------------------------------------------- #
# execution.sync_live_balance — happy path
# --------------------------------------------------------------------------- #
def test_sync_live_balance_fresh_sets_cash_and_baseline(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    monkeypatch.setattr(ex, "get_live_balance", lambda: {"balance_usd": 321.0})

    res = ex.sync_live_balance("live")
    assert res == {"status": "synced", "cash": 321.0, "set_starting": True}

    # Local cash now equals the real wallet balance, and because the live
    # portfolio had no trades the baseline was pegged too -> conservation holds.
    p = state.get_portfolio("live")
    assert p["cash"] == 321.0
    assert p["starting_balance"] == 321.0
    assert p["realized_pnl"] == 0.0
    assert p["open_exposure_usd"] == 0.0
    assert p["open_positions"] == 0


def test_sync_live_balance_defaults_to_live_mode(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    monkeypatch.setattr(ex, "get_live_balance", lambda: {"balance_usd": 42.0})
    res = ex.sync_live_balance()  # mode defaults to "live"
    assert res["status"] == "synced"
    assert state.get_portfolio("live")["cash"] == 42.0
    # Paper untouched.
    assert state.get_portfolio("paper")["cash"] == 200.0


def test_sync_live_balance_second_sync_after_trade_only_updates_cash(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    # First sync on a fresh portfolio establishes BOTH cash and baseline.
    monkeypatch.setattr(ex, "get_live_balance", lambda: {"balance_usd": 300.0})
    first = ex.sync_live_balance("live")
    assert first == {"status": "synced", "cash": 300.0, "set_starting": True}
    assert state.get_portfolio("live")["starting_balance"] == 300.0

    # A real trade happens (debits cash, bumps trade_count).
    state.open_position("live", market_id="M1", token_id="t1", question="q",
        category="c", side="YES", entry_price=0.5, size_usd=20.0, shares=40.0,
        fair_prob=0.5, confidence=0.5, thesis="t")
    assert state.get_portfolio("live")["trade_count"] == 1

    # Second sync: wallet shows a different balance. Cash is trued-up, but the
    # baseline is left intact (set_starting False because trades now exist).
    monkeypatch.setattr(ex, "get_live_balance", lambda: {"balance_usd": 250.0})
    second = ex.sync_live_balance("live")
    assert second == {"status": "synced", "cash": 250.0, "set_starting": False}

    p = state.get_portfolio("live")
    assert p["cash"] == 250.0
    assert p["starting_balance"] == 300.0   # baseline UNCHANGED by the true-up
    assert p["trade_count"] == 1


def test_sync_live_balance_zero_balance_still_syncs(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    # A real, numeric zero balance is a valid anchor (drained wallet), not an
    # "unavailable" read.
    monkeypatch.setattr(ex, "get_live_balance", lambda: {"balance_usd": 0.0})
    res = ex.sync_live_balance("live")
    assert res == {"status": "synced", "cash": 0.0, "set_starting": True}
    assert state.get_portfolio("live")["cash"] == 0.0


# --------------------------------------------------------------------------- #
# execution.sync_live_balance — failure paths (never raise, no state change)
# --------------------------------------------------------------------------- #
def test_sync_live_balance_no_key_is_error_and_no_state_change(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "")
    before = state.get_portfolio("live")

    res = ex.sync_live_balance("live")
    assert res["status"] == "error"
    assert "PRIVATE_KEY" in res["reason"]

    # Cash/baseline are unchanged (still the seeded starting balance).
    after = state.get_portfolio("live")
    assert after["cash"] == before["cash"] == 200.0
    assert after["starting_balance"] == before["starting_balance"] == 200.0


def test_sync_live_balance_balance_error_is_error_and_no_state_change(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    monkeypatch.setattr(ex, "get_live_balance",
        lambda: {"error": "RuntimeError: api down", "balance_usd": None})

    res = ex.sync_live_balance("live")
    assert res["status"] == "error"
    assert "api down" in res["reason"]
    # No anchoring happened.
    assert state.get_portfolio("live")["cash"] == 200.0
    assert state.get_portfolio("live")["starting_balance"] == 200.0


def test_sync_live_balance_non_numeric_balance_is_unavailable(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    # Balance read "succeeded" (no error key) but carried no usable number.
    monkeypatch.setattr(ex, "get_live_balance", lambda: {"balance_usd": None})

    res = ex.sync_live_balance("live")
    assert res["status"] == "unavailable"
    assert state.get_portfolio("live")["cash"] == 200.0


def test_sync_live_balance_get_balance_raising_is_caught(env, monkeypatch):
    state, ex = env
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")

    def _boom():
        raise RuntimeError("balance fetch blew up")

    monkeypatch.setattr(ex, "get_live_balance", _boom)

    # Must NOT raise.
    res = ex.sync_live_balance("live")
    assert res["status"] == "error"
    assert "balance fetch blew up" in res["reason"]
    # No anchoring happened.
    assert state.get_portfolio("live")["cash"] == 200.0
    assert state.get_portfolio("live")["starting_balance"] == 200.0
