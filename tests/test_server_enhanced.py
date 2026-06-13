# tests/test_server_enhanced.py
"""Hermetic coverage for the Phase-15 server-layer enhancements:

  - get_market routes through markets.fetch_market (single direct lookup)
  - get_portfolio best-effort mark-to-market enrichment (1 position, 0 positions)
  - get_performance_stats shape
  - get_session_report shape
  - preflight_live wrapper shape

All network access is monkeypatched; with zero open positions get_portfolio must
make NO network call (the common test path).
"""
import importlib

import pytest

from polymarket_mcp.markets import Market


@pytest.fixture
def srv(tmp_path, monkeypatch):
    """Reload config/db/state/server against an isolated paper data dir with a
    known starting balance so portfolio/state assertions are deterministic."""
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_MODE", "paper")
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "100")
    import polymarket_mcp.config as cfg
    import polymarket_mcp.db as db
    import polymarket_mcp.state as state
    import polymarket_mcp.server as server
    for m in (cfg, db, state, server):
        importlib.reload(m)
    return server


# --------------------------------------------------------------------------- #
# get_market -> fetch_market
# --------------------------------------------------------------------------- #
def test_get_market_uses_fetch_market(srv, monkeypatch):
    market = Market(
        condition_id="cid42", question="Will X happen?", category="crypto",
        yes_price=0.6, no_price=0.4, volume=1234.0, end_date="2026-12-31",
        active=True, tokens=[{"token_id": "tY", "outcome": "Yes", "price": 0.6},
                             {"token_id": "tN", "outcome": "No", "price": 0.4}])
    calls = {}

    def fake_fetch_market(condition_id):
        calls["id"] = condition_id
        return market

    monkeypatch.setattr(srv._markets, "fetch_market", fake_fetch_market)
    # The old O(n) scan must NOT be used.
    monkeypatch.setattr(srv._markets, "fetch_active_markets",
        lambda *a, **k: pytest.fail("get_market must not scan fetch_active_markets"))

    out = srv.get_market("cid42")
    assert calls["id"] == "cid42"
    assert out["market_id"] == "cid42"
    assert out["question"] == "Will X happen?"
    assert out["tokens"][0]["token_id"] == "tY"


def test_get_market_not_found(srv, monkeypatch):
    monkeypatch.setattr(srv._markets, "fetch_market", lambda condition_id: None)
    out = srv.get_market("missing")
    assert out == {"error": "market missing not found"}


# --------------------------------------------------------------------------- #
# get_portfolio mark-to-market enrichment
# --------------------------------------------------------------------------- #
def test_get_portfolio_zero_positions_no_network(srv, monkeypatch):
    # With no open positions, the order book must NEVER be fetched.
    monkeypatch.setattr(srv._order_book, "fetch_order_book",
        lambda tid: pytest.fail("no network call expected with zero positions"))
    out = srv.get_portfolio()
    # Base keys present, no enrichment keys.
    assert out["cash"] == 100.0
    assert out["open_positions"] == 0
    assert "unrealized_pnl" not in out
    assert "mark_to_market_value" not in out
    assert "total_pnl" not in out


def test_get_portfolio_unrealized_enrichment(srv, monkeypatch):
    # Open one position: 40 shares for $20 (entry 0.50). Mark at midpoint 0.60.
    srv._state.open_position("paper", market_id="m1", token_id="tok",
        question="Q", category="crypto", side="YES", entry_price=0.50,
        size_usd=20.0, shares=40.0, fair_prob=0.6, confidence=0.7, thesis="t")
    monkeypatch.setattr(srv._order_book, "fetch_order_book",
        lambda tid: {"midpoint": 0.60, "best_bid": 0.59, "best_ask": 0.61})

    out = srv.get_portfolio()
    # mark value = 40 * 0.60 = 24.0 ; cost basis = 20.0 ; unrealized = +4.0
    assert out["mark_to_market_value"] == 24.0
    assert out["unrealized_pnl"] == 4.0
    # realized_pnl is 0 on a fresh portfolio with one open position.
    assert out["total_pnl"] == round(out["realized_pnl"] + 4.0, 2)
    assert out["marked_positions"] == 1


def test_get_portfolio_per_position_best_effort(srv, monkeypatch):
    # Two positions: one has a usable midpoint, one does not. Enrichment must
    # mark the good one and SKIP the bad one (not abort everything).
    srv._state.open_position("paper", market_id="m1", token_id="good",
        question="Q1", category="crypto", side="YES", entry_price=0.50,
        size_usd=20.0, shares=40.0, fair_prob=0.6, confidence=0.7, thesis="t")
    srv._state.open_position("paper", market_id="m2", token_id="bad",
        question="Q2", category="crypto", side="YES", entry_price=0.50,
        size_usd=10.0, shares=20.0, fair_prob=0.6, confidence=0.7, thesis="t")

    def fake_book(tid):
        if tid == "good":
            return {"midpoint": 0.60, "best_bid": 0.59, "best_ask": 0.61}
        return {"error": "one-sided book", "midpoint": 0.0}

    monkeypatch.setattr(srv._order_book, "fetch_order_book", fake_book)
    out = srv.get_portfolio()
    # Only the good position is marked: value 40 * 0.60 = 24.0, cost 20.0.
    assert out["marked_positions"] == 1
    assert out["mark_to_market_value"] == 24.0
    assert out["unrealized_pnl"] == 4.0


def test_get_portfolio_enrichment_degrades_on_book_error(srv, monkeypatch):
    srv._state.open_position("paper", market_id="m1", token_id="tok",
        question="Q", category="crypto", side="YES", entry_price=0.50,
        size_usd=20.0, shares=40.0, fair_prob=0.6, confidence=0.7, thesis="t")
    # A book with no usable midpoint (one-sided / error) must not break the call.
    monkeypatch.setattr(srv._order_book, "fetch_order_book",
        lambda tid: {"error": "order book fetch failed", "midpoint": 0.0})
    out = srv.get_portfolio()
    assert out["cash"] == 80.0  # base portfolio still returned
    assert "unrealized_pnl" not in out
    assert "mark_to_market_value" not in out
    assert "marked_positions" not in out  # nothing markable -> no enrichment


# --------------------------------------------------------------------------- #
# get_performance_stats shape
# --------------------------------------------------------------------------- #
def test_get_performance_stats_shape_empty(srv):
    stats = srv.get_performance_stats()
    assert set(stats.keys()) == {"overall", "by_category"}
    overall = stats["overall"]
    for key in ("count", "wins", "losses", "win_rate", "total_pnl", "avg_pnl", "roi"):
        assert key in overall
    assert overall["count"] == 0
    assert stats["by_category"] == {}


def test_get_performance_stats_passthrough(srv, monkeypatch):
    sentinel = {"overall": {"count": 3}, "by_category": {"crypto": {"count": 3}}}
    captured = {}

    def fake_stats(category=None):
        captured["category"] = category
        return sentinel

    monkeypatch.setattr(srv._state, "get_performance_stats", fake_stats)
    out = srv.get_performance_stats(category="crypto")
    assert out is sentinel
    assert captured["category"] == "crypto"


# --------------------------------------------------------------------------- #
# get_session_report shape
# --------------------------------------------------------------------------- #
def test_get_session_report_shape(srv, monkeypatch):
    # Zero positions -> get_portfolio makes no network call; everything else is
    # backed by the isolated paper DB.
    report = srv.get_session_report()
    assert set(report.keys()) == {
        "portfolio", "risk", "performance", "recent_trades", "open_positions"}
    assert report["portfolio"]["cash"] == 100.0
    assert "breaker_tripped" in report["risk"]
    assert set(report["performance"].keys()) == {"overall", "by_category"}
    # recent_trades is wrapped {"trades": [...]} to match get_trade_history.
    assert isinstance(report["recent_trades"], dict)
    assert isinstance(report["recent_trades"]["trades"], list)
    assert report["open_positions"] == 0


# --------------------------------------------------------------------------- #
# preflight_live shape
# --------------------------------------------------------------------------- #
def test_preflight_live_shape_no_key(srv, monkeypatch):
    # No key configured -> ok False, checks present, never raises, no live trade.
    monkeypatch.setattr(srv._execution.config, "POLYMARKET_PRIVATE_KEY", "")
    out = srv.preflight_live()
    assert set(out.keys()) == {"ok", "checks"}
    assert out["ok"] is False
    assert "private_key_set" in out["checks"]
    assert out["checks"]["private_key_set"] is False
