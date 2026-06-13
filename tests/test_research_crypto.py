import json
from pathlib import Path

import polymarket_mcp.research.crypto as crypto

FIX = Path(__file__).parent / "fixtures"


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
    # _maybe_prediction must swallow the missing crypto_model import and omit it.
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
