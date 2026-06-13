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


# --------------------------------------------------------------------------- #
# send_notification wrapper
# --------------------------------------------------------------------------- #
def test_send_notification_no_op_when_unconfigured(srv):
    # No webhook / Telegram configured -> strict no-op, no network, no raise.
    out = srv.send_notification("fill", "bought YES on m1")
    assert out == {"sent": False, "reason": "no channel configured"}


def test_send_notification_forwards_event_and_message(srv, monkeypatch):
    captured = {}

    def fake_notify(event, message, **fields):
        captured["event"] = event
        captured["message"] = message
        return {"sent": True, "channels": [{"channel": "webhook", "ok": True}]}

    monkeypatch.setattr(srv._notifications, "notify", fake_notify)
    out = srv.send_notification("kill_switch", "breaker tripped")
    assert captured == {"event": "kill_switch", "message": "breaker tripped"}
    assert out["sent"] is True


# --------------------------------------------------------------------------- #
# record_evaluation tool forwards stake_usd
# --------------------------------------------------------------------------- #
def test_record_evaluation_persists_stake_usd(srv):
    # Record with a stake, then read the row back from the DB directly.
    out = srv.record_evaluation(market_id="m1", question="Q", category="crypto",
        keywords="btc", decision="buy_yes", side="YES", confidence=0.7,
        fair_prob=0.62, yes_price=0.55, thesis="momentum", stake_usd=25.0)
    assert out["status"] == "ok"
    conn = srv._state.db.connect("paper")
    try:
        row = conn.execute(
            "SELECT stake_usd FROM market_intelligence WHERE id=?",
            (out["id"],)).fetchone()
    finally:
        conn.close()
    assert row["stake_usd"] == 25.0


def test_record_evaluation_stake_drives_roi(srv):
    # End-to-end: record a staked evaluation, resolve it with a profit, and
    # confirm get_performance_stats reports stake-based ROI (pnl / stake).
    out = srv.record_evaluation(market_id="m1", question="Q", category="crypto",
        keywords="btc", decision="buy_yes", side="YES", confidence=0.7,
        fair_prob=0.62, yes_price=0.55, thesis="momentum", stake_usd=20.0)
    assert out["status"] == "ok"
    # Resolve the row with +10 PnL on a 20 stake -> ROI 0.5.
    srv._state.update_evaluation_outcome("paper", "m1", "YES", 1.0, 10.0)
    stats = srv.get_performance_stats()
    assert stats["overall"]["total_stake"] == 20.0
    assert stats["overall"]["roi"] == 0.5


# --------------------------------------------------------------------------- #
# list_markets binary_only filter
# --------------------------------------------------------------------------- #
def test_list_markets_binary_only_filters_multi_outcome(srv, monkeypatch):
    binary = Market(
        condition_id="b1", question="Will BTC top 100k?", category="crypto",
        yes_price=0.6, no_price=0.4, volume=5000.0, end_date="2026-12-31",
        active=True,
        tokens=[{"token_id": "tY", "outcome": "Yes", "price": 0.6},
                {"token_id": "tN", "outcome": "No", "price": 0.4}],
        is_binary=True)
    multi = Market(
        condition_id="m1", question="Who wins the cup?", category="sports",
        yes_price=0.5, no_price=0.5, volume=9000.0, end_date="2026-12-31",
        active=True,
        tokens=[{"token_id": "tA", "outcome": "Team A", "price": 0.5},
                {"token_id": "tB", "outcome": "Team B", "price": 0.3},
                {"token_id": "tC", "outcome": "Team C", "price": 0.2}],
        is_binary=False)
    monkeypatch.setattr(srv._markets, "fetch_active_markets",
                        lambda *a, **k: [multi, binary])

    # Default: both markets returned, multi-outcome data flows via `outcomes`.
    all_out = srv.list_markets()
    ids = {m["market_id"] for m in all_out["markets"]}
    assert ids == {"b1", "m1"}
    multi_dict = next(m for m in all_out["markets"] if m["market_id"] == "m1")
    assert multi_dict["is_binary"] is False
    assert len(multi_dict["outcomes"]) == 3
    assert {o["outcome"] for o in multi_dict["outcomes"]} == {"Team A", "Team B", "Team C"}

    # binary_only=True: the multi-outcome market is filtered out.
    bin_out = srv.list_markets(binary_only=True)
    assert bin_out["count"] == 1
    assert bin_out["markets"][0]["market_id"] == "b1"
    assert bin_out["markets"][0]["is_binary"] is True


# --------------------------------------------------------------------------- #
# get_scorecard -> replay.scorecard (seeded resolved-eval DB)
# --------------------------------------------------------------------------- #
def test_get_scorecard_shape_empty(srv):
    # Fresh paper DB: no resolved trades -> fully-zeroed overall bucket, empty
    # by_category, and no divide-by-zero / math-domain error.
    sc = srv.get_scorecard()
    assert set(sc.keys()) == {"overall", "by_category"}
    overall = sc["overall"]
    for key in ("count", "wins", "losses", "win_rate", "total_pnl",
                "total_stake", "roi", "brier", "log_loss", "calibration"):
        assert key in overall
    assert overall["count"] == 0
    assert overall["calibration"] != []  # ten stable decile buckets always present
    assert len(overall["calibration"]) == 10
    assert sc["by_category"] == {}


def test_get_scorecard_over_seeded_resolved_trades(srv):
    # Seed two resolved BUY trades in the isolated paper DB: one win, one loss,
    # then confirm the scorecard reflects win-rate / ROI / probabilistic metrics.
    # Winner: buy_yes, fair_prob 0.7, +12 PnL on a 20 stake.
    w = srv._state.record_evaluation("paper", market_id="mw", question="W",
        category="crypto", keywords="btc", decision="buy_yes", side="YES",
        confidence=0.8, fair_prob=0.7, yes_price=0.55, thesis="t", stake_usd=20.0)
    assert isinstance(w, int) and w > 0
    srv._state.update_evaluation_outcome("paper", "mw", "YES", 1.0, 12.0)
    # Loser: buy_no, fair_prob 0.6 (chosen-side prob 0.4), -10 PnL on a 10 stake.
    li = srv._state.record_evaluation("paper", market_id="ml", question="L",
        category="crypto", keywords="eth", decision="buy_no", side="NO",
        confidence=0.6, fair_prob=0.6, yes_price=0.45, thesis="t", stake_usd=10.0)
    assert isinstance(li, int) and li > 0
    srv._state.update_evaluation_outcome("paper", "ml", "NO", 0.0, -10.0)

    sc = srv.get_scorecard()
    overall = sc["overall"]
    assert overall["count"] == 2
    assert overall["wins"] == 1
    assert overall["losses"] == 1
    assert overall["win_rate"] == 0.5
    # ROI = net PnL / summed stake = (12 - 10) / (20 + 10) = 2 / 30.
    assert overall["total_pnl"] == 2.0
    assert overall["total_stake"] == 30.0
    assert overall["roi"] == round(2.0 / 30.0, 4)
    # Probabilistic metrics computed over both rows (both carry a fair_prob).
    assert overall["brier"] > 0.0
    assert overall["log_loss"] > 0.0
    # Both trades are crypto -> a single by_category bucket mirrors overall.
    assert set(sc["by_category"]) == {"crypto"}
    assert sc["by_category"]["crypto"]["count"] == 2


def test_get_scorecard_delegates_to_replay(srv, monkeypatch):
    sentinel = {"overall": {"count": 7}, "by_category": {}}
    called = {}

    def fake_scorecard():
        called["hit"] = True
        return sentinel

    monkeypatch.setattr(srv._replay, "scorecard", fake_scorecard)
    out = srv.get_scorecard()
    assert out is sentinel
    assert called["hit"] is True


# --------------------------------------------------------------------------- #
# list_resolved_markets -> replay.fetch_resolved_markets (monkeypatched)
# --------------------------------------------------------------------------- #
def test_list_resolved_markets_shape(srv, monkeypatch):
    rows = [
        {"market_id": "r1", "question": "Did X happen?", "category": "crypto",
         "winning_outcome": "Yes"},
        {"market_id": "r2", "question": "Who won?", "category": "sports",
         "winning_outcome": None},
    ]
    captured = {}

    def fake_fetch(limit):
        captured["limit"] = limit
        return rows

    monkeypatch.setattr(srv._replay, "fetch_resolved_markets", fake_fetch)
    out = srv.list_resolved_markets(limit=5)
    assert captured["limit"] == 5
    assert out["count"] == 2
    assert out["markets"] == rows
    assert out["markets"][0]["winning_outcome"] == "Yes"
    assert out["markets"][1]["winning_outcome"] is None


def test_list_resolved_markets_empty_on_fetch_failure(srv, monkeypatch):
    # replay.fetch_resolved_markets is best-effort and returns [] on failure;
    # the wrapper must surface that as a clean, count=0 shape (never raise).
    monkeypatch.setattr(srv._replay, "fetch_resolved_markets", lambda limit: [])
    out = srv.list_resolved_markets()
    assert out == {"count": 0, "markets": []}


# --------------------------------------------------------------------------- #
# Live-ops wrappers: graceful no-key / error shapes by default (no network)
# --------------------------------------------------------------------------- #
def test_get_open_orders_no_key_error(srv, monkeypatch):
    # No private key -> error dict, never a raise, no network call.
    monkeypatch.setattr(srv._execution.config, "POLYMARKET_PRIVATE_KEY", "")
    out = srv.get_open_orders()
    assert "error" in out
    assert "orders" not in out


def test_cancel_order_no_key_error(srv, monkeypatch):
    monkeypatch.setattr(srv._execution.config, "POLYMARKET_PRIVATE_KEY", "")
    out = srv.cancel_order("abc123")
    assert out["status"] == "error"
    assert "reason" in out


def test_cancel_order_rejects_blank_id(srv, monkeypatch):
    # Even with a key set, a blank order_id must be rejected before any call.
    monkeypatch.setattr(srv._execution.config, "POLYMARKET_PRIVATE_KEY", "0xdead")
    out = srv.cancel_order("   ")
    assert out["status"] == "error"
    assert "reason" in out


def test_import_live_positions_no_key_error(srv, monkeypatch):
    monkeypatch.setattr(srv._execution.config, "POLYMARKET_PRIVATE_KEY", "")
    out = srv.import_live_positions()
    assert "error" in out
    assert out["adopted"] == 0
    assert out["skipped"] == 0
