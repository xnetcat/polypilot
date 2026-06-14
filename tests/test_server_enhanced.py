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
# get_market_signals -> signals.market_signals (monkeypatched)
# --------------------------------------------------------------------------- #
def test_get_market_signals_delegates_to_signals(srv, monkeypatch):
    sentinel = {"token_id": "tok", "momentum": 0.5, "trend": "up",
                "summary": "up trend"}
    captured = {}

    def fake_market_signals(token_id):
        captured["token_id"] = token_id
        return sentinel

    monkeypatch.setattr(srv._signals, "market_signals", fake_market_signals)
    out = srv.get_market_signals("tok")
    assert out is sentinel
    assert captured["token_id"] == "tok"


def test_get_market_signals_surfaces_error_shape(srv, monkeypatch):
    monkeypatch.setattr(srv._signals, "market_signals",
        lambda token_id: {"error": "no price history", "token_id": token_id})
    out = srv.get_market_signals("tok")
    assert out == {"error": "no price history", "token_id": "tok"}


# --------------------------------------------------------------------------- #
# calibrate_probability -> replay.calibrate_probability
# --------------------------------------------------------------------------- #
def test_calibrate_probability_thin_history_falls_back_to_raw(srv):
    # Fresh paper DB: no resolved trades -> insufficient history -> calibrated == raw.
    out = srv.calibrate_probability(0.62)
    assert out["raw"] == 0.62
    assert out["calibrated"] == 0.62
    assert out["samples"] == 0
    assert out["base_rate"] is None
    assert "insufficient history" in out["note"]


def test_calibrate_probability_calibrates_over_seeded_bucket(srv):
    # Seed >=5 resolved buy_yes trades whose fair_prob sits in the 0.6-0.7 decile,
    # with 3 wins / 2 losses. The bucket's empirical win rate is 0.6 and the global
    # base rate is also 0.6 (all trades are in this one bucket), so beta-shrinkage
    # toward the base rate leaves calibrated exactly at 0.6: (3 + k·0.6)/(5 + k) = 0.6.
    outcomes = [(1.0, 12.0), (1.0, 12.0), (1.0, 12.0), (0.0, -10.0), (0.0, -10.0)]
    for i, (res, pnl) in enumerate(outcomes):
        mid = f"m{i}"
        srv._state.record_evaluation("paper", market_id=mid, question="Q",
            category="crypto", keywords="btc", decision="buy_yes", side="YES",
            confidence=0.7, fair_prob=0.65, yes_price=0.6, thesis="t", stake_usd=10.0)
        srv._state.update_evaluation_outcome("paper", mid, "YES", res, pnl)

    out = srv.calibrate_probability(0.62)  # 0.62 lands in the 0.6-0.7 bucket
    assert out["raw"] == 0.62
    assert out["samples"] == 5
    assert out["calibrated"] == 0.6
    assert out["base_rate"] == 0.6
    assert "beta-shrunk toward base rate" in out["note"]


def test_calibrate_probability_shrinks_sparse_bucket_toward_base(srv):
    # 10 resolved trades; the SPARSE high bucket (0.9-1.0) has just 1 win out of 1.
    # Naive empirical would be 1.0, but beta-shrinkage toward the global base rate
    # must pull it strictly below 1.0, proving shrinkage is active.
    # Bucket 0.6-0.7: 6 buy_yes @0.65, all WIN.
    for i in range(6):
        mid = f"a{i}"
        srv._state.record_evaluation("paper", market_id=mid, question="Q",
            category="crypto", keywords="k", decision="buy_yes", side="YES",
            confidence=0.7, fair_prob=0.65, yes_price=0.6, thesis="t", stake_usd=10.0)
        srv._state.update_evaluation_outcome("paper", mid, "YES", 1.0, 6.0)
    # Bucket 0.3-0.4: 3 buy_yes @0.35, all LOSE.
    for i in range(3):
        mid = f"b{i}"
        srv._state.record_evaluation("paper", market_id=mid, question="Q",
            category="crypto", keywords="k", decision="buy_yes", side="YES",
            confidence=0.4, fair_prob=0.35, yes_price=0.35, thesis="t", stake_usd=10.0)
        srv._state.update_evaluation_outcome("paper", mid, "YES", 0.0, -10.0)
    # Bucket 0.9-1.0: a single WIN (neighbor 0.8-0.9 is empty).
    srv._state.record_evaluation("paper", market_id="c0", question="Q",
        category="crypto", keywords="k", decision="buy_yes", side="YES",
        confidence=0.95, fair_prob=0.95, yes_price=0.9, thesis="t", stake_usd=10.0)
    srv._state.update_evaluation_outcome("paper", "c0", "YES", 1.0, 1.0)

    out = srv.calibrate_probability(0.95)  # lands in the sparse 0.9-1.0 bucket
    assert out["raw"] == 0.95
    assert out["samples"] == 1                 # one in-bucket trade
    assert out["base_rate"] == 0.7             # 7 wins / 10 resolved
    # (wins + k·base)/(total + k) = (1 + 5·0.7)/(1 + 5) = 0.75 — between base and 1.0.
    assert out["calibrated"] == 0.75


def test_calibrate_probability_delegates_to_replay(srv, monkeypatch):
    sentinel = {"raw": 0.5, "calibrated": 0.55, "samples": 9, "note": "ok"}
    captured = {}

    def fake_calibrate(fair_prob):
        captured["fair_prob"] = fair_prob
        return sentinel

    monkeypatch.setattr(srv._replay, "calibrate_probability", fake_calibrate)
    out = srv.calibrate_probability(0.5)
    assert out is sentinel
    assert captured["fair_prob"] == 0.5


# --------------------------------------------------------------------------- #
# notify_status -> portfolio/risk summary -> notifications.notify
# --------------------------------------------------------------------------- #
def test_notify_status_no_op_when_unconfigured(srv, monkeypatch):
    # No webhook / Telegram configured (conftest wiped them) -> strict no-op:
    # no network call (httpx.post must never fire), and the no-op shape returned.
    monkeypatch.setattr(srv._notifications.httpx, "post",
        lambda *a, **k: pytest.fail("no network call expected when unconfigured"))
    # Zero open positions -> get_portfolio also makes no order-book network call.
    monkeypatch.setattr(srv._order_book, "fetch_order_book",
        lambda tid: pytest.fail("no network call expected with zero positions"))
    out = srv.notify_status()
    assert out == {"sent": False, "reason": "no channel configured"}


def test_notify_status_composes_pnl_summary(srv, monkeypatch):
    captured = {}

    def fake_notify(event, message, **fields):
        captured["event"] = event
        captured["message"] = message
        captured["fields"] = fields
        return {"sent": True, "channels": [{"channel": "webhook", "ok": True}]}

    monkeypatch.setattr(srv._notifications, "notify", fake_notify)
    out = srv.notify_status()
    assert out["sent"] is True
    assert captured["event"] == "status"
    assert captured["message"] == "Portfolio update"
    # Composed from get_portfolio()/risk_status(): fresh paper portfolio.
    fields = captured["fields"]
    assert fields["cash"] == 100.0
    assert fields["realized_pnl"] == 0.0
    assert fields["open_positions"] == 0
    assert fields["mode"] == "paper"
    # Keys are always present even when unrealized enrichment is absent.
    assert set(fields) == {"cash", "realized_pnl", "unrealized_pnl",
                           "total_pnl", "open_positions", "mode"}


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


def test_list_markets_closing_within_hours_filter(srv, monkeypatch):
    import datetime

    def _iso(hours_from_now):
        return (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(hours=hours_from_now)).strftime("%Y-%m-%dT%H:%M:%SZ")

    soon = Market(
        condition_id="soon", question="Resolves soon?", category="crypto",
        yes_price=0.5, no_price=0.5, volume=5000.0, end_date=_iso(6),
        active=True, tokens=[{"token_id": "t", "outcome": "Yes", "price": 0.5}])
    far = Market(
        condition_id="far", question="Resolves later?", category="crypto",
        yes_price=0.5, no_price=0.5, volume=9000.0, end_date=_iso(72),
        active=True, tokens=[{"token_id": "t", "outcome": "Yes", "price": 0.5}])
    monkeypatch.setattr(srv._markets, "fetch_active_markets",
                        lambda *a, **k: [far, soon])

    # No filter -> both; each dict carries hours_to_close.
    all_out = srv.list_markets()
    assert {m["market_id"] for m in all_out["markets"]} == {"soon", "far"}
    assert all(m["hours_to_close"] is not None for m in all_out["markets"])

    # closing_within_hours=24 -> only the market resolving in ~6h.
    near = srv.list_markets(closing_within_hours=24)
    assert near["count"] == 1
    assert near["markets"][0]["market_id"] == "soon"


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


def test_get_live_account_no_key_error(srv, monkeypatch):
    # No private key -> error shape, never a raise, and no network call (the
    # snapshot bails on the key check before reading balance/positions).
    monkeypatch.setattr(srv._execution.config, "POLYMARKET_PRIVATE_KEY", "")
    monkeypatch.setattr(srv._execution, "_get_clob_client",
        lambda: pytest.fail("no clob client expected with no key"))
    monkeypatch.setattr(srv._order_book, "fetch_order_book",
        lambda tid: pytest.fail("no order book fetch expected with no key"))
    out = srv.get_live_account()
    assert out["status"] == "error"
    assert "PRIVATE_KEY" in out["reason"]
    assert "balance_usd" not in out
    assert "positions" not in out


def test_sync_live_balance_no_key_error(srv, monkeypatch):
    # No private key -> error shape, never a raise, and no network call.
    monkeypatch.setattr(srv._execution.config, "POLYMARKET_PRIVATE_KEY", "")
    monkeypatch.setattr(srv._execution, "_get_clob_client",
        lambda: pytest.fail("no clob client expected with no key"))
    out = srv.sync_live_balance()
    assert out["status"] == "error"
    assert "PRIVATE_KEY" in out["reason"]


# --------------------------------------------------------------------------- #
# Live-balance enrichment of the safety views is hermetic in paper/no-key env
# --------------------------------------------------------------------------- #
def test_get_risk_status_no_live_balance_in_paper(srv, monkeypatch):
    # Default fixture env is paper + no key: get_risk_status must NOT gain a
    # live_balance_usd, must make NO network call, and must never raise.
    monkeypatch.setattr(srv._execution, "_get_clob_client",
        lambda: pytest.fail("no clob client expected in paper/no-key env"))
    out = srv.get_risk_status()
    assert out["mode"] == "paper"
    assert "live_balance_usd" not in out
    assert out["breaker_tripped"] is False


def test_get_portfolio_no_live_balance_in_paper(srv, monkeypatch):
    # Paper + no key: get_portfolio must NOT add live_balance_usd and must make
    # NO live-balance network call (zero positions already skips marking).
    monkeypatch.setattr(srv._execution, "_get_clob_client",
        lambda: pytest.fail("no clob client expected in paper/no-key env"))
    monkeypatch.setattr(srv._order_book, "fetch_order_book",
        lambda tid: pytest.fail("no network call expected in paper/no-key env"))
    out = srv.get_portfolio()
    assert out["cash"] == 100.0
    assert "live_balance_usd" not in out


# --------------------------------------------------------------------------- #
# get_esports -> esports.fetch_esports
# --------------------------------------------------------------------------- #
def test_get_esports_no_key_returns_error_without_network(srv, monkeypatch):
    # conftest wipes POLYPILOT_PANDASCORE_KEY, so the reloaded config has no key:
    # a detected-game question must return the no-key error dict and make NO
    # network call (the module bails on the key check before any httpx.get).
    monkeypatch.setattr(srv._esports.config, "PANDASCORE_KEY", "")
    monkeypatch.setattr(srv._esports.httpx, "get",
        lambda *a, **k: pytest.fail("no network call expected without a key"))
    out = srv.get_esports("Will NAVI beat G2 in CS2?")
    assert out["game"] == "csgo"
    assert out["error"] == "POLYPILOT_PANDASCORE_KEY not set"
    assert out["source"] == "pandascore"


def test_get_esports_no_game_detected(srv):
    # A non-esports question yields the no-game error and never touches the net.
    out = srv.get_esports("Will it rain in Seattle tomorrow?")
    assert out == {"error": "no esports game detected",
                   "question": "Will it rain in Seattle tomorrow?"}


def test_get_esports_delegates_to_fetch_esports(srv, monkeypatch):
    sentinel = {"game": "dota2", "team1": "OG", "team2": "Liquid",
                "status": "running", "source": "pandascore"}
    captured = {}

    def fake_fetch(question):
        captured["question"] = question
        return sentinel

    monkeypatch.setattr(srv._esports, "fetch_esports", fake_fetch)
    out = srv.get_esports("OG vs Liquid Dota 2")
    assert out is sentinel
    assert captured["question"] == "OG vs Liquid Dota 2"


# --------------------------------------------------------------------------- #
# compute_edge: V2 price-dependent fee
# --------------------------------------------------------------------------- #
def test_compute_edge_reports_price_dependent_fee(srv):
    out = srv.compute_edge(0.60, "YES", 0.45)
    assert out["fee"] == 0.0099                  # 0.04*(0.45*0.55)
    assert out["expected_value"] == 0.1401


def test_compute_edge_fee_free_market(srv):
    out = srv.compute_edge(0.60, "YES", 0.45, fee_rate=0.0)
    assert out["fee"] == 0.0
    assert out["expected_value"] == 0.15


# --------------------------------------------------------------------------- #
# compute_edge_from_book: edge at the real ask
# --------------------------------------------------------------------------- #
def test_compute_edge_from_book_uses_best_ask(srv, monkeypatch):
    # fetch_order_book returns a summary DICT (best_ask/midpoint/spread/...).
    monkeypatch.setattr(srv._order_book, "fetch_order_book", lambda tid: {
        "token_id": tid, "best_bid": 0.80, "best_ask": 0.84, "midpoint": 0.82,
        "spread": 0.04, "asks": [{"price": 0.84, "size": 25}]})
    out = srv.compute_edge_from_book("tok", fair_prob=0.92, fee_rate=0.04)
    assert out["best_ask"] == 0.84
    # fee at 0.84 = 0.04*(0.84*0.16)=0.0054 ; EV = (0.92-0.84) - 0.0054 = 0.0746
    assert out["fee"] == 0.0054
    assert out["expected_value"] == 0.0746


def test_compute_edge_from_book_no_asks_errors(srv, monkeypatch):
    monkeypatch.setattr(srv._order_book, "fetch_order_book",
                        lambda tid: {"token_id": tid, "best_ask": None, "asks": []})
    out = srv.compute_edge_from_book("tok", fair_prob=0.9)
    assert "error" in out


# --------------------------------------------------------------------------- #
# place_order: limit 5-share minimum pre-check
# --------------------------------------------------------------------------- #
def test_place_order_limit_below_5_shares_rejected_precheck(srv):
    # 2 USD / 0.83 = 2.41 shares < 5 -> rejected up front (no fill, no network).
    out = srv.place_order(market_id="m1", token_id="tok", question="Q",
        category="weather", side="NO", size_usd=2.0, order_type="limit",
        limit_price=0.83)
    assert out["status"] == "rejected"
    assert "minimum" in out["reason"] and "market order" in out["reason"]


def test_place_order_limit_at_5_shares_passes_precheck(srv, monkeypatch):
    # 4.20 USD / 0.84 = 5.0 shares -> clears the floor; with a stubbed book (the
    # summary-dict shape _paper_fill consumes) the paper limit then fills.
    monkeypatch.setattr(srv._order_book, "fetch_order_book", lambda tid: {
        "token_id": tid, "best_bid": 0.82, "best_ask": 0.84, "spread": 0.02,
        "midpoint": 0.83, "asks": [{"price": 0.84, "size": 100}]})
    out = srv.place_order(market_id="m1", token_id="tok", question="Q",
        category="weather", side="NO", size_usd=4.20, order_type="limit",
        limit_price=0.84)
    assert out["status"] == "filled"


# --------------------------------------------------------------------------- #
# suggest_size: order-minimum awareness with a price
# --------------------------------------------------------------------------- #
def test_suggest_size_with_price_flags_limit_floor(srv):
    out = srv.suggest_size(edge=0.04, bankroll=11.69, price=0.84)
    assert out["min_limit_usd"] == 4.2
    assert out["limit_placeable_at_suggested"] is False
    assert "note" in out


def test_suggest_size_without_price_is_bare(srv):
    out = srv.suggest_size(edge=0.1, bankroll=100.0)
    assert out == {"suggested_usd": 5.0}


# --------------------------------------------------------------------------- #
# check_data_sources -> diagnostics.check_data_sources
# --------------------------------------------------------------------------- #
class _DiagResp:
    def __init__(self, status_code=200):
        self.status_code = status_code


def test_check_data_sources_probe_false_makes_no_network(srv, monkeypatch):
    # probe=False must be a pure config readout — zero network calls.
    monkeypatch.setattr(srv._diagnostics.httpx, "get",
        lambda *a, **k: pytest.fail("no network call expected with probe=False"))
    out = srv.check_data_sources(probe=False)
    assert out["probed"] is False
    names = {s["name"] for s in out["sources"]}
    assert {"coingecko", "binance", "open-meteo", "fred", "pandascore"} <= names
    assert all(s["reachable"] is None for s in out["sources"])
    assert out["summary"]["total"] == len(out["sources"])


def test_check_data_sources_probes_keyless_and_skips_unconfigured_keys(srv, monkeypatch):
    # conftest wipes research keys, so fred/pandascore/newsapi are unconfigured.
    monkeypatch.setattr(srv._diagnostics.httpx, "get",
                        lambda url, **k: _DiagResp(200))
    out = srv.check_data_sources(probe=True)
    assert out["probed"] is True
    by_name = {s["name"]: s for s in out["sources"]}

    # Keyless sources get probed and report reachable on a 2xx.
    for name in ("coingecko", "binance", "open-meteo", "weather.gov", "espn", "rss"):
        assert by_name[name]["requires_key"] is False
        assert by_name[name]["reachable"] is True
        assert by_name[name]["status"] == 200

    # Keyed-but-unconfigured sources are reported, NOT probed.
    for name in ("fred", "pandascore", "newsapi"):
        assert by_name[name]["requires_key"] is True
        assert by_name[name]["key_configured"] is False
        assert by_name[name]["reachable"] is None

    assert out["summary"]["keyed_configured"] == 0
    assert out["summary"]["reachable"] == 6


def test_check_data_sources_probes_configured_key(srv, monkeypatch):
    # With a PandaScore key configured, that source is probed too.
    monkeypatch.setattr(srv._diagnostics.config, "PANDASCORE_KEY", "k-123")
    monkeypatch.setattr(srv._diagnostics.httpx, "get",
                        lambda url, **k: _DiagResp(200))
    out = srv.check_data_sources(probe=True)
    by_name = {s["name"]: s for s in out["sources"]}
    assert by_name["pandascore"]["key_configured"] is True
    assert by_name["pandascore"]["reachable"] is True
    assert out["summary"]["keyed_configured"] == 1


def test_check_data_sources_marks_unreachable_on_error(srv, monkeypatch):
    # A probe that raises must be caught and reported as not reachable, no raise.
    def _boom(*a, **k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(srv._diagnostics.httpx, "get", _boom)
    out = srv.check_data_sources(probe=True)
    by_name = {s["name"]: s for s in out["sources"]}
    assert by_name["coingecko"]["reachable"] is False
    assert "connection refused" in str(by_name["coingecko"]["status"])
    assert out["summary"]["reachable"] == 0
