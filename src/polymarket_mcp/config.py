"""Centralized configuration from environment variables (POLYPILOT_* prefix)."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# --- Mode ---
_raw_mode = (os.getenv("POLYPILOT_MODE", "paper") or "paper").strip().lower()
MODE: str = _raw_mode if _raw_mode in {"paper", "live"} else "paper"

# --- Paper bankroll ---
STARTING_BALANCE_USD: float = _float("POLYPILOT_STARTING_BALANCE_USD", 200.0)

# --- Kill-switch (the ONLY hard guardrail) ---
KILL_SWITCH_DAILY_USD: float = _float("POLYPILOT_KILL_SWITCH_DAILY_USD", 50.0)
KILL_SWITCH_TOTAL_USD: float = _float("POLYPILOT_KILL_SWITCH_TOTAL_USD", 100.0)

# --- Data directory ---
DATA_DIR: str = os.getenv("POLYPILOT_DATA_DIR", "")

# --- Polymarket connection (live trading) ---
POLYMARKET_HOST: str = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
POLYMARKET_PRIVATE_KEY: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_PROXY_ADDRESS: str = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
POLYMARKET_SIGNATURE_TYPE: int = _int("POLYMARKET_SIGNATURE_TYPE", 0)
HTTP_PROXY: str = os.getenv("POLYPILOT_HTTP_PROXY", "")
GAMMA_API: str = "https://gamma-api.polymarket.com"

# --- Optional research API keys ---
FRED_API_KEY: str = os.getenv("FRED_API_KEY", "")
NEWSAPI_KEY: str = os.getenv("NEWSAPI_KEY", "")
