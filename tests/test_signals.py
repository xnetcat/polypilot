"""Tests for technical market signals (no network)."""
import statistics

import polymarket_mcp.signals as signals


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _patch_history(monkeypatch, prices):
    payload = {"history": [{"t": i, "p": str(p)} for i, p in enumerate(prices)]}
    monkeypatch.setattr(signals.httpx, "get", lambda *a, **k: _FakeResp(payload))


def _patch_book(monkeypatch, book):
    monkeypatch.setattr(signals.order_book, "fetch_order_book", lambda tid: book)


def test_momentum_zscore_and_trend(monkeypatch):
    # Uptrend that pulls back on the final point.
    prices = [0.50, 0.52, 0.55, 0.60, 0.58]
    _patch_history(monkeypatch, prices)
    _patch_book(monkeypatch, {"imbalance": 0.10, "spread": 0.02, "midpoint": 0.57})

    result = signals.market_signals("tok")

    # momentum = z-score of the most recent delta against all deltas (pstdev).
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    mean = statistics.fmean(deltas)
    stdev = statistics.pstdev(deltas)
    expected_momentum = (deltas[-1] - mean) / stdev
    assert result["momentum"] == round(expected_momentum, 4)
    assert result["momentum"] < 0  # final delta is negative -> below-mean momentum

    # first -> last is 0.50 -> 0.58, an up trend with positive price change.
    assert result["trend"] == "up"
    assert result["price_change"] == round(0.58 - 0.50, 4)
    assert result["points"] == 5
    assert "up trend" in result["summary"]


def test_pullback_from_high_on_uptrend(monkeypatch):
    prices = [0.50, 0.60, 0.55]  # high 0.60, low 0.50, last 0.55
    _patch_history(monkeypatch, prices)
    _patch_book(monkeypatch, {"imbalance": 0.0, "spread": 0.0, "midpoint": 0.0})

    result = signals.market_signals("tok")

    # retracement from high: (high - current) / (high - low) = (0.60-0.55)/(0.60-0.50)
    assert result["pullback"] == round((0.60 - 0.55) / (0.60 - 0.50), 4)


def test_bullish_divergence(monkeypatch):
    # Price falling (down trend) but order book bid-heavy (positive imbalance).
    prices = [0.60, 0.55, 0.50]
    _patch_history(monkeypatch, prices)
    _patch_book(monkeypatch, {"imbalance": 0.30, "spread": 0.01, "midpoint": 0.50})

    result = signals.market_signals("tok")

    assert result["trend"] == "down"
    assert result["price_change"] < 0
    assert result["divergence"] == "bullish"
    assert "bullish divergence" in result["summary"]


def test_bearish_divergence(monkeypatch):
    # Price rising but order book ask-heavy (negative imbalance).
    prices = [0.40, 0.45, 0.50]
    _patch_history(monkeypatch, prices)
    _patch_book(monkeypatch, {"imbalance": -0.25, "spread": 0.01, "midpoint": 0.50})

    result = signals.market_signals("tok")

    assert result["trend"] == "up"
    assert result["price_change"] > 0
    assert result["divergence"] == "bearish"


def test_momentum_zero_with_few_points(monkeypatch):
    prices = [0.50, 0.52]  # < 3 points -> momentum 0
    _patch_history(monkeypatch, prices)
    _patch_book(monkeypatch, {"imbalance": 0.0, "spread": 0.0, "midpoint": 0.0})

    result = signals.market_signals("tok")

    assert result["momentum"] == 0.0
    assert result["divergence"] == "none"


def test_price_history_fetch_error_returns_error_dict(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(signals.httpx, "get", boom)
    # book should never be consulted; make it loud if it is.
    _patch_book(monkeypatch, {"imbalance": 0.0})

    result = signals.market_signals("tok")

    assert "error" in result
    assert "connection refused" in result["error"]
    assert result["token_id"] == "tok"


def test_order_book_error_propagates(monkeypatch):
    _patch_history(monkeypatch, [0.50, 0.52, 0.55])
    _patch_book(monkeypatch, {"error": "order book fetch failed: boom", "token_id": "tok"})

    result = signals.market_signals("tok")

    assert "error" in result
    assert "order book fetch failed" in result["error"]


def test_empty_history_returns_error(monkeypatch):
    _patch_history(monkeypatch, [])
    _patch_book(monkeypatch, {"imbalance": 0.0})

    result = signals.market_signals("tok")

    assert result["error"] == "no price history"
