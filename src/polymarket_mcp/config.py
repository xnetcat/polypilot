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
# When a proxy is configured, export the STANDARD proxy env vars *in this
# process* so httpx (which honors HTTPS_PROXY/HTTP_PROXY via trust_env) routes
# ALL Polymarket calls — order placement AND data reads — through it. This
# mirrors the proven predecessor implementation and is more robust than patching
# py_clob_client's client alone. Scoped to this process (the MCP server), so it
# does NOT proxy the host Claude Code session's own traffic (hence the custom
# POLYPILOT_HTTP_PROXY trigger rather than a global HTTP_PROXY).
if HTTP_PROXY and not os.getenv("HTTPS_PROXY"):
    os.environ["HTTPS_PROXY"] = HTTP_PROXY
    os.environ["HTTP_PROXY"] = HTTP_PROXY
GAMMA_API: str = "https://gamma-api.polymarket.com"

# --- Optional research API keys ---
FRED_API_KEY: str = os.getenv("FRED_API_KEY", "")
NEWSAPI_KEY: str = os.getenv("NEWSAPI_KEY", "")
PANDASCORE_KEY: str = os.getenv("POLYPILOT_PANDASCORE_KEY", "")

# --- Optional notifications (best-effort; all unset -> notifier is a no-op) ---
# Generic webhook: a JSON {event, message, ...} is POSTed here.
POLYPILOT_WEBHOOK_URL: str = os.getenv("POLYPILOT_WEBHOOK_URL", "")
# Telegram bot: both the token AND chat id must be set to enable Telegram.
POLYPILOT_TELEGRAM_BOT_TOKEN: str = os.getenv("POLYPILOT_TELEGRAM_BOT_TOKEN", "")
POLYPILOT_TELEGRAM_CHAT_ID: str = os.getenv("POLYPILOT_TELEGRAM_CHAT_ID", "")
