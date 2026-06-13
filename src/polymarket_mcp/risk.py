"""The single hard guardrail: a cumulative-loss kill-switch."""
from __future__ import annotations

from . import config, state


def check_kill_switch(mode: str | None = None) -> tuple[bool, str]:
    """Return (allowed, reason). Trips when day or total loss meets the limit."""
    p = state.get_portfolio(mode)
    total_loss = max(0.0, -p["realized_pnl"])
    day_loss = max(0.0, -p["day_realized_pnl"])
    if total_loss >= config.KILL_SWITCH_TOTAL_USD:
        return False, (f"KILL SWITCH: total loss ${total_loss:.2f} >= "
                       f"limit ${config.KILL_SWITCH_TOTAL_USD:.2f}")
    if day_loss >= config.KILL_SWITCH_DAILY_USD:
        return False, (f"KILL SWITCH: daily loss ${day_loss:.2f} >= "
                       f"limit ${config.KILL_SWITCH_DAILY_USD:.2f}")
    return True, ""


def risk_status(mode: str | None = None) -> dict:
    p = state.get_portfolio(mode)
    ok, reason = check_kill_switch(mode)
    return {
        "mode": p["mode"],
        "cash": p["cash"],
        "realized_pnl": p["realized_pnl"],
        "day_realized_pnl": p["day_realized_pnl"],
        "open_exposure_usd": p["open_exposure_usd"],
        "kill_switch_daily_usd": config.KILL_SWITCH_DAILY_USD,
        "kill_switch_total_usd": config.KILL_SWITCH_TOTAL_USD,
        "breaker_tripped": not ok,
        "halt_reason": reason,
    }
