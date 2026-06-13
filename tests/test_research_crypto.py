import json
import math
from pathlib import Path

import polymarket_mcp.research.crypto as crypto

FIX = Path(__file__).parent / "fixtures"

# Spot used across the probability tests (mid-band for the $64k market).
_SPOT = 64000.0


def _alternating_price_series(start: float = 50000.0, r: float = 0.03, n: int = 30) -> list[float]:
    """31 prices whose daily log-returns alternate +r/-r (a known sigma).

    The sample stdev (ddof=1) of these 30 returns is deterministic, so tests
    can hand-compute the lognormal probability and assert to a tight tolerance.
    """
    prices = [start]
    sign = 1
    for _ in range(n):
        prices.append(prices[-1] * math.exp(r * sign))
        sign *= -1
    return prices


def _expected_daily_sigma(prices: list[float]) -> float:
    rets = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var)


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _hand_prob_band(spot, low, high, sigma, horizon):
    sigma_t = sigma * math.sqrt(horizon)
    zl = (math.log(low) - math.log(spot)) / sigma_t
    zh = (math.log(high) - math.log(spot)) / sigma_t
    return _phi(zh) - _phi(zl)


def _spot_response(spot: float):
    """A fake httpx response for the /simple/price spot endpoint."""

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"bitcoin": {"usd": spot, "usd_24h_change": 0.2, "usd_24h_vol": 1.0}}

    return R()


def test_fetch_crypto_returns_spot(monkeypatch):
    crypto._CACHE.clear()
    data = json.loads((FIX / "coingecko_btc.json").read_text())

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return data

    monkeypatch.setattr(crypto.httpx, "get", lambda *a, **k: R())
    result = crypto.fetch_crypto("Will Bitcoin be above $70k?")
    assert result["symbol"] in ("BTC", "bitcoin")
    assert result["spot_usd"] > 0
    # Crypto research has no ML prediction; the key must never be present.
    assert "prediction" not in result


def test_fetch_crypto_target_threshold(monkeypatch):
    crypto._CACHE.clear()
    data = {"bitcoin": {"usd": 63951, "usd_24h_change": 0.21, "usd_24h_vol": 1.0}}

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return data

    monkeypatch.setattr(crypto.httpx, "get", lambda *a, **k: R())
    result = crypto.fetch_crypto("Will Bitcoin be above $70000?")
    assert "target" in result
    assert result["target"]["price"] == 70000.0
    assert result["target"]["direction"] == "above"


def test_fetch_crypto_no_asset_returns_error():
    crypto._CACHE.clear()
    result = crypto.fetch_crypto("Will it rain in Tokyo tomorrow?")
    assert "error" in result


# --- band/threshold probability -------------------------------------------


def _wire_probability(monkeypatch, spot=_SPOT, horizon=7):
    """Pin spot, the daily price series, and the horizon for deterministic math."""
    crypto._CACHE.clear()
    prices = _alternating_price_series()
    monkeypatch.setattr(crypto.httpx, "get", lambda *a, **k: _spot_response(spot))
    monkeypatch.setattr(crypto, "_fetch_daily_prices", lambda coin_id: list(prices))
    # Pin horizon so the result doesn't depend on today's date.
    monkeypatch.setattr(crypto, "_parse_horizon_days", lambda q, today: (horizon, None))
    return prices


def test_band_probability_matches_hand_computed(monkeypatch):
    prices = _wire_probability(monkeypatch, horizon=7)
    sigma = _expected_daily_sigma(prices)
    expected = _hand_prob_band(_SPOT, 63500.0, 64500.0, sigma, 7)

    result = crypto.fetch_crypto(
        "Will Bitcoin be between $63,500 and $64,500?"
    )
    prob = result["probability"]
    assert prob["kind"] == "band"
    assert prob["low"] == 63500.0
    assert prob["high"] == 64500.0
    assert prob["model"] == "lognormal"
    assert prob["horizon_days"] == 7
    assert prob["daily_vol"] == round(sigma, 6)
    assert prob["estimate"] == round(max(0.0, min(1.0, expected)), 6)
    assert abs(prob["estimate"] - expected) < 1e-4


def test_narrow_band_yields_small_probability(monkeypatch):
    _wire_probability(monkeypatch, horizon=7)
    result = crypto.fetch_crypto(
        "Will Bitcoin be between $63,950 and $64,050?"
    )
    prob = result["probability"]
    assert prob["kind"] == "band"
    # A $100-wide band around a ~7.6% sigma_T move is very unlikely.
    assert 0.0 < prob["estimate"] < 0.05


def test_above_far_below_spot_yields_high_probability(monkeypatch):
    _wire_probability(monkeypatch, horizon=7)
    # Threshold $30k is far below the $64k spot, so "above" is near-certain.
    result = crypto.fetch_crypto("Will Bitcoin be above $30,000?")
    prob = result["probability"]
    assert prob["kind"] == "above"
    assert prob["threshold"] == 30000.0
    assert prob["estimate"] > 0.99


def test_below_far_above_spot_yields_high_probability(monkeypatch):
    _wire_probability(monkeypatch, horizon=7)
    # Spot $64k is far below $90k, so "below $90k" is near-certain.
    result = crypto.fetch_crypto("Will Bitcoin be below $90,000?")
    prob = result["probability"]
    assert prob["kind"] == "below"
    assert prob["threshold"] == 90000.0
    assert prob["estimate"] > 0.99


def test_no_series_omits_probability_without_raising(monkeypatch):
    crypto._CACHE.clear()
    monkeypatch.setattr(crypto.httpx, "get", lambda *a, **k: _spot_response(_SPOT))
    # Empty series -> no volatility estimate -> probability omitted.
    monkeypatch.setattr(crypto, "_fetch_daily_prices", lambda coin_id: [])

    result = crypto.fetch_crypto(
        "Will Bitcoin be between $63,500 and $64,500 on June 20?"
    )
    # Spot is still present; probability is simply absent (no exception).
    assert result["spot_usd"] == _SPOT
    assert "probability" not in result


def test_single_point_series_omits_probability(monkeypatch):
    crypto._CACHE.clear()
    monkeypatch.setattr(crypto.httpx, "get", lambda *a, **k: _spot_response(_SPOT))
    monkeypatch.setattr(crypto, "_fetch_daily_prices", lambda coin_id: [64000.0])

    result = crypto.fetch_crypto("Will Bitcoin be above $30,000?")
    assert "probability" not in result


def test_probability_never_raises_on_fetch_error(monkeypatch):
    crypto._CACHE.clear()
    monkeypatch.setattr(crypto.httpx, "get", lambda *a, **k: _spot_response(_SPOT))

    def boom(coin_id):
        raise RuntimeError("network down")

    monkeypatch.setattr(crypto, "_fetch_daily_prices", boom)
    # _fetch_daily_prices itself guards, but assert the outer path is safe too.
    result = crypto.fetch_crypto("Will Bitcoin be above $30,000?")
    assert "error" not in result
    assert result["spot_usd"] == _SPOT
    assert "probability" not in result


def test_band_parse_with_k_suffix(monkeypatch):
    _wire_probability(monkeypatch, horizon=7)
    result = crypto.fetch_crypto("Will Bitcoin be between $63k and $65k?")
    prob = result["probability"]
    assert prob["kind"] == "band"
    assert prob["low"] == 63000.0
    assert prob["high"] == 65000.0


def test_parse_horizon_from_date():
    import datetime

    today = datetime.date(2026, 6, 14)
    days, note = crypto._parse_horizon_days(
        "Will Bitcoin be above $70,000 on June 20?", today
    )
    assert days == 6
    assert note is None


def test_parse_horizon_defaults_with_note():
    import datetime

    today = datetime.date(2026, 6, 14)
    days, note = crypto._parse_horizon_days("Will Bitcoin be above $70,000?", today)
    assert days == crypto.DEFAULT_HORIZON_DAYS
    assert note is not None
