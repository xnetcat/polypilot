"""Test-suite baseline: a clean, hermetic environment.

This dev machine exports real Polypilot config for live trading (mode=live,
wallet key, Brazil proxy, Discord webhook, FRED key, ...). None of that must
leak into the test process — it would break "unconfigured" assertions and, worse,
make tests issue real network calls (to Polymarket / Discord / Telegram).

So before any config import we wipe ALL ambient POLYPILOT_*/POLYMARKET_* vars
plus the standard proxy and research-key vars, then pin paper mode. Tests that
exercise a specific config set it themselves via monkeypatch + importlib.reload.
"""
import os

for _k in list(os.environ):
    if _k.startswith("POLYPILOT_") or _k.startswith("POLYMARKET_"):
        os.environ.pop(_k, None)

for _v in ("HTTP_PROXY", "HTTPS_PROXY", "FRED_API_KEY", "NEWSAPI_KEY"):
    os.environ.pop(_v, None)

os.environ["POLYPILOT_MODE"] = "paper"
