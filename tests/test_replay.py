"""Coverage for polymarket_mcp.replay — the backtesting/replay scorecard.

``scorecard`` reads RESOLVED ``market_intelligence`` rows that were actual
trades (decision buy_yes/buy_no) and reports win rate, ROI, Brier, log loss and
a per-decile calibration table, overall and per category. ``fetch_resolved_markets``
pulls recently CLOSED markets from Gamma and infers the winning outcome.

Uses the standard reload fixture (config -> db -> state pick up the patched env
and db._INITIALIZED is cleared) so the temp DB is isolated per test. All network
is monkeypatched; nothing here touches the wire.
"""
import importlib
import json
import math

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Reloaded (config, db, state, replay) bound to a temp DB dir."""
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYPILOT_STARTING_BALANCE_USD", "200")
    import polymarket_mcp.config as cfg
    import polymarket_mcp.db as db
    import polymarket_mcp.state as state
    import polymarket_mcp.replay as replay
    for m in (cfg, db, state, replay):
        importlib.reload(m)
    return type("Env", (), {"cfg": cfg, "db": db, "state": state, "replay": replay})


def _record_resolved(env, *, market_id, category, decision, fair_prob, pnl,
                     stake=0.0, mode="paper"):
    """Record a trade evaluation then resolve it with the given pnl.

    `decision` is buy_yes/buy_no; `fair_prob` is the model's P(YES). The
    outcome_side / resolution_price set on resolution are cosmetic for the
    scorecard (it keys off pnl sign and decision/fair_prob), so we set
    plausible values."""
    env.state.record_evaluation(
        mode, market_id=market_id, question=f"q {market_id}", category=category,
        keywords="k", decision=decision, side=("YES" if decision == "buy_yes" else "NO"),
        confidence=0.6, fair_prob=fair_prob, yes_price=0.5, thesis="t",
        stake_usd=stake)
    won = pnl > 0
    env.state.update_evaluation_outcome(
        mode, market_id, "YES" if won else "NO", 1.0 if won else 0.0, pnl)


def _insert_unresolved_trade(env, *, market_id, decision, fair_prob, mode="paper"):
    env.state.record_evaluation(
        mode, market_id=market_id, question="q", category="c", keywords="k",
        decision=decision, side="YES", confidence=0.6, fair_prob=fair_prob,
        yes_price=0.5, thesis="t", stake_usd=10.0)


# --------------------------------------------------------------------------
# scorecard
# --------------------------------------------------------------------------

def test_scorecard_empty_returns_zeros(env):
    sc = env.replay.scorecard("paper")
    assert sc["by_category"] == {}
    o = sc["overall"]
    assert o["count"] == 0
    assert o["wins"] == 0
    assert o["losses"] == 0
    assert o["win_rate"] == 0.0
    assert o["total_pnl"] == 0.0
    assert o["total_stake"] == 0.0
    assert o["roi"] == 0.0
    assert o["brier"] == 0.0
    assert o["log_loss"] == 0.0
    # Empty calibration: 10 buckets, all zero.
    assert len(o["calibration"]) == 10
    assert all(b["count"] == 0 for b in o["calibration"])
    assert sum(b["count"] for b in o["calibration"]) == 0


def test_scorecard_win_rate_and_roi(env):
    # Three resolved trades: 2 wins, 1 loss.
    #   w1: buy_yes pnl +20 on $40 stake
    #   w2: buy_no  pnl +10 on $20 stake
    #   l1: buy_yes pnl -15 on $30 stake
    # total_pnl = 15, total_stake = 90, roi = 15/90
    _record_resolved(env, market_id="w1", category="weather", decision="buy_yes",
                     fair_prob=0.7, pnl=20.0, stake=40.0)
    _record_resolved(env, market_id="w2", category="weather", decision="buy_no",
                     fair_prob=0.3, pnl=10.0, stake=20.0)
    _record_resolved(env, market_id="l1", category="crypto", decision="buy_yes",
                     fair_prob=0.6, pnl=-15.0, stake=30.0)

    o = env.replay.scorecard("paper")["overall"]
    assert o["count"] == 3
    assert o["wins"] == 2
    assert o["losses"] == 1
    assert o["win_rate"] == round(2 / 3, 4)
    assert o["total_pnl"] == 15.0
    assert o["total_stake"] == 90.0
    assert o["roi"] == round(15.0 / 90.0, 4)


def test_scorecard_brier_hand_computed(env):
    # Hand-compute Brier over 3 trades. Predicted prob = P(chosen side wins):
    #   t1: buy_yes fair 0.80, WIN  -> pred 0.80, actual 1 -> (0.80-1)^2 = 0.04
    #   t2: buy_no  fair 0.30, WIN  -> pred 1-0.30=0.70, actual 1 -> 0.09
    #   t3: buy_yes fair 0.60, LOSS -> pred 0.60, actual 0 -> 0.36
    # Brier = (0.04 + 0.09 + 0.36) / 3 = 0.49/3 = 0.163333...
    _record_resolved(env, market_id="t1", category="c", decision="buy_yes",
                     fair_prob=0.80, pnl=5.0, stake=10.0)
    _record_resolved(env, market_id="t2", category="c", decision="buy_no",
                     fair_prob=0.30, pnl=5.0, stake=10.0)
    _record_resolved(env, market_id="t3", category="c", decision="buy_yes",
                     fair_prob=0.60, pnl=-5.0, stake=10.0)

    o = env.replay.scorecard("paper")["overall"]
    expected = (0.04 + 0.09 + 0.36) / 3
    assert o["brier"] == round(expected, 6)


def test_scorecard_log_loss_hand_computed_and_finite(env):
    # Same 3 trades as the Brier test. Log loss = mean(-(a ln p + (1-a) ln(1-p))).
    #   t1: a=1 p=0.80 -> -ln(0.80)
    #   t2: a=1 p=0.70 -> -ln(0.70)
    #   t3: a=0 p=0.60 -> -ln(1-0.60)=-ln(0.40)
    _record_resolved(env, market_id="t1", category="c", decision="buy_yes",
                     fair_prob=0.80, pnl=5.0, stake=10.0)
    _record_resolved(env, market_id="t2", category="c", decision="buy_no",
                     fair_prob=0.30, pnl=5.0, stake=10.0)
    _record_resolved(env, market_id="t3", category="c", decision="buy_yes",
                     fair_prob=0.60, pnl=-5.0, stake=10.0)

    o = env.replay.scorecard("paper")["overall"]
    expected = (-math.log(0.80) - math.log(0.70) - math.log(0.40)) / 3
    assert o["log_loss"] == round(expected, 6)
    assert math.isfinite(o["log_loss"])


def test_scorecard_log_loss_no_domain_error_at_extremes(env):
    # fair_prob at the hard extremes (0.0 / 1.0) would make ln(p) or ln(1-p)
    # blow up if unclamped. The scorecard must clamp to [1e-6, 1-1e-6] and stay
    # finite for every combination of extreme prob and win/loss.
    _record_resolved(env, market_id="a", category="c", decision="buy_yes",
                     fair_prob=1.0, pnl=5.0, stake=10.0)   # pred 1.0, win
    _record_resolved(env, market_id="b", category="c", decision="buy_yes",
                     fair_prob=1.0, pnl=-5.0, stake=10.0)  # pred 1.0, loss
    _record_resolved(env, market_id="c", category="c", decision="buy_yes",
                     fair_prob=0.0, pnl=5.0, stake=10.0)   # pred 0.0, win
    _record_resolved(env, market_id="d", category="c", decision="buy_no",
                     fair_prob=1.0, pnl=5.0, stake=10.0)   # pred 1-1=0.0, win

    o = env.replay.scorecard("paper")["overall"]
    assert math.isfinite(o["log_loss"])
    assert math.isfinite(o["brier"])
    assert o["log_loss"] > 0


def test_scorecard_calibration_buckets_sum_to_count(env):
    # Predictions spread across deciles; the calibration counts must sum to the
    # number of probability-bearing rows (here all 4 trades carry fair_prob).
    _record_resolved(env, market_id="m1", category="c", decision="buy_yes",
                     fair_prob=0.05, pnl=-1.0, stake=5.0)   # bucket 0.0-0.1
    _record_resolved(env, market_id="m2", category="c", decision="buy_yes",
                     fair_prob=0.55, pnl=2.0, stake=5.0)    # bucket 0.5-0.6
    _record_resolved(env, market_id="m3", category="c", decision="buy_yes",
                     fair_prob=0.55, pnl=-2.0, stake=5.0)   # bucket 0.5-0.6
    _record_resolved(env, market_id="m4", category="c", decision="buy_yes",
                     fair_prob=0.95, pnl=3.0, stake=5.0)    # bucket 0.9-1.0

    o = env.replay.scorecard("paper")["overall"]
    cal = o["calibration"]
    assert len(cal) == 10
    assert sum(b["count"] for b in cal) == o["count"] == 4

    by_bucket = {b["bucket"]: b for b in cal}
    # 0.5-0.6 bucket: two preds at 0.55, one win one loss -> mean 0.55, win 0.5
    mid = by_bucket["0.5-0.6"]
    assert mid["count"] == 2
    assert mid["mean_predicted"] == 0.55
    assert mid["actual_win_rate"] == 0.5
    # 0.9-1.0 bucket: one winning pred at 0.95
    top = by_bucket["0.9-1.0"]
    assert top["count"] == 1
    assert top["mean_predicted"] == 0.95
    assert top["actual_win_rate"] == 1.0


def test_scorecard_calibration_top_edge_lands_in_last_bucket(env):
    # A predicted prob of exactly 1.0 must land in the 0.9-1.0 bucket, not
    # overflow past the table.
    _record_resolved(env, market_id="x", category="c", decision="buy_yes",
                     fair_prob=1.0, pnl=5.0, stake=10.0)
    cal = env.replay.scorecard("paper")["overall"]["calibration"]
    by_bucket = {b["bucket"]: b for b in cal}
    assert by_bucket["0.9-1.0"]["count"] == 1
    assert sum(b["count"] for b in cal) == 1


def test_scorecard_by_category_breakdown(env):
    # weather: 1 win (+20/$40), 1 loss (-5/$10) -> count 2, roi 15/50
    # crypto:  1 win (+10/$20)                  -> count 1, roi 10/20
    _record_resolved(env, market_id="w1", category="weather", decision="buy_yes",
                     fair_prob=0.7, pnl=20.0, stake=40.0)
    _record_resolved(env, market_id="w2", category="weather", decision="buy_no",
                     fair_prob=0.4, pnl=-5.0, stake=10.0)
    _record_resolved(env, market_id="c1", category="crypto", decision="buy_yes",
                     fair_prob=0.6, pnl=10.0, stake=20.0)

    sc = env.replay.scorecard("paper")
    assert set(sc["by_category"]) == {"weather", "crypto"}

    weather = sc["by_category"]["weather"]
    assert weather["count"] == 2
    assert weather["wins"] == 1
    assert weather["losses"] == 1
    assert weather["total_pnl"] == 15.0
    assert weather["total_stake"] == 50.0
    assert weather["roi"] == round(15.0 / 50.0, 4)

    crypto = sc["by_category"]["crypto"]
    assert crypto["count"] == 1
    assert crypto["wins"] == 1
    assert crypto["roi"] == round(10.0 / 20.0, 4)


def test_scorecard_ignores_skips_and_unresolved(env):
    # A resolved SKIP and an unresolved trade must not count.
    # Resolved skip:
    env.state.record_evaluation("paper", market_id="skip1", question="q",
        category="c", keywords="k", decision="skip", side="", confidence=0.5,
        fair_prob=0.5, yes_price=0.5, thesis="t", stake_usd=0.0)
    env.state.update_evaluation_outcome("paper", "skip1", "YES", 1.0, 0.0)
    # Unresolved trade:
    _insert_unresolved_trade(env, market_id="open1", decision="buy_yes",
                             fair_prob=0.6)
    # One real resolved trade:
    _record_resolved(env, market_id="t1", category="c", decision="buy_yes",
                     fair_prob=0.7, pnl=5.0, stake=10.0)

    o = env.replay.scorecard("paper")["overall"]
    assert o["count"] == 1
    assert o["wins"] == 1


def test_scorecard_null_fair_prob_skipped_in_brier_but_counts_in_winrate(env):
    # A resolved trade with NULL fair_prob still counts toward win-rate/PnL/ROI
    # but is excluded from Brier/log-loss/calibration (no prediction to score).
    _record_resolved(env, market_id="hasprob", category="c", decision="buy_yes",
                     fair_prob=0.8, pnl=5.0, stake=10.0)
    _record_resolved(env, market_id="noprob", category="c", decision="buy_yes",
                     fair_prob=None, pnl=5.0, stake=10.0)

    o = env.replay.scorecard("paper")["overall"]
    assert o["count"] == 2          # both count
    assert o["wins"] == 2
    # Only the row WITH a fair_prob is in calibration / Brier.
    assert sum(b["count"] for b in o["calibration"]) == 1
    # Brier over the single scored row: (0.8-1)^2 = 0.04
    assert o["brier"] == round(0.04, 6)


def test_scorecard_roi_guarded_when_no_stake(env):
    # A resolved trade with zero stake must not cause a divide-by-zero; roi 0.0.
    _record_resolved(env, market_id="z", category="c", decision="buy_yes",
                     fair_prob=0.6, pnl=5.0, stake=0.0)
    o = env.replay.scorecard("paper")["overall"]
    assert o["count"] == 1
    assert o["total_stake"] == 0.0
    assert o["roi"] == 0.0


def test_scorecard_is_json_serializable(env):
    _record_resolved(env, market_id="t1", category="weather", decision="buy_yes",
                     fair_prob=0.7, pnl=5.0, stake=10.0)
    sc = env.replay.scorecard("paper")
    assert json.loads(json.dumps(sc)) == sc


# --------------------------------------------------------------------------
# fetch_resolved_markets
# --------------------------------------------------------------------------

CLOSED_YES_WINS = {
    "question": "Will it rain in NYC on June 1?",
    "conditionId": "0xrain",
    "outcomes": "[\"Yes\", \"No\"]",
    "outcomePrices": "[\"1\", \"0\"]",
    "volume": "50000",
    "closed": True,
    "tags": ["weather"],
}

CLOSED_NO_WINS = {
    "question": "Will Bitcoin hit $200k by June?",
    "conditionId": "0xbtc",
    "outcomes": "[\"Yes\", \"No\"]",
    "outcomePrices": "[\"0\", \"1\"]",
    "volume": "80000",
    "closed": True,
    "tags": ["crypto"],
}

CLOSED_UNDETERMINED = {
    # Both ~0.5: no clear winner -> winning_outcome None.
    "question": "Coin flip market?",
    "conditionId": "0xflip",
    "outcomes": "[\"Heads\", \"Tails\"]",
    "outcomePrices": "[\"0.5\", \"0.5\"]",
    "volume": "1000",
    "closed": True,
    "tags": [],
}


def _make_resp(payload):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    return FakeResp()


def test_fetch_resolved_markets_parses_and_detects_winner(env, monkeypatch):
    monkeypatch.setattr(
        env.replay.httpx, "get",
        lambda *a, **k: _make_resp([CLOSED_YES_WINS, CLOSED_NO_WINS]))

    out = env.replay.fetch_resolved_markets(limit=10)
    assert len(out) == 2

    rain = out[0]
    assert rain["market_id"] == "0xrain"
    assert rain["question"] == "Will it rain in NYC on June 1?"
    assert rain["category"] == "weather"
    assert rain["winning_outcome"] == "Yes"

    btc = out[1]
    assert btc["market_id"] == "0xbtc"
    assert btc["category"] == "crypto"
    assert btc["winning_outcome"] == "No"


def test_fetch_resolved_markets_undetermined_winner_is_none(env, monkeypatch):
    monkeypatch.setattr(
        env.replay.httpx, "get",
        lambda *a, **k: _make_resp([CLOSED_UNDETERMINED]))
    out = env.replay.fetch_resolved_markets()
    assert len(out) == 1
    assert out[0]["winning_outcome"] is None


def test_fetch_resolved_markets_uses_closed_filter_and_order(env, monkeypatch):
    captured = {}

    def fake_get(url, *args, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return _make_resp([CLOSED_YES_WINS])

    monkeypatch.setattr(env.replay.httpx, "get", fake_get)
    env.replay.fetch_resolved_markets(limit=7)

    assert captured["url"] == f"{env.replay.config.GAMMA_API}/markets"
    p = captured["params"]
    assert p["closed"] is True
    assert p["limit"] == 7
    assert p["order"] == "volume"
    assert p["ascending"] is False


def test_fetch_resolved_markets_handles_dict_envelope(env, monkeypatch):
    monkeypatch.setattr(
        env.replay.httpx, "get",
        lambda *a, **k: _make_resp({"data": [CLOSED_YES_WINS]}))
    out = env.replay.fetch_resolved_markets()
    assert len(out) == 1
    assert out[0]["market_id"] == "0xrain"


def test_fetch_resolved_markets_network_error_returns_empty(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(env.replay.httpx, "get", boom)
    assert env.replay.fetch_resolved_markets() == []


def test_fetch_resolved_markets_skips_bad_rows(env, monkeypatch):
    # A row missing question/id is skipped; a row with malformed outcomePrices
    # still parses (just yields winning_outcome None).
    bad_missing_id = {"question": "no id here", "outcomePrices": "[\"1\",\"0\"]"}
    malformed_prices = {
        "question": "Malformed prices market",
        "conditionId": "0xbad",
        "outcomes": "[\"Yes\", \"No\"]",
        "outcomePrices": "not-json",
        "closed": True,
    }
    monkeypatch.setattr(
        env.replay.httpx, "get",
        lambda *a, **k: _make_resp([bad_missing_id, malformed_prices, CLOSED_YES_WINS]))

    out = env.replay.fetch_resolved_markets()
    ids = {r["market_id"] for r in out}
    assert "0xrain" in ids
    assert "0xbad" in ids                      # parsed despite bad prices
    assert all(r["market_id"] for r in out)    # no empty ids slipped through
    bad = next(r for r in out if r["market_id"] == "0xbad")
    assert bad["winning_outcome"] is None       # couldn't determine a winner


def test_fetch_resolved_markets_empty_list(env, monkeypatch):
    monkeypatch.setattr(env.replay.httpx, "get", lambda *a, **k: _make_resp([]))
    assert env.replay.fetch_resolved_markets() == []
