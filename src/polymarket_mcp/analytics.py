"""Advisory trading math: expected value, ROI, Kelly sizing.
These are TOOLS the agent may call to sanity-check its own numbers — NOT gates."""
from __future__ import annotations

POLYMARKET_FEE_PCT = 0.02


def expected_value(fair_prob: float, side: str, yes_price: float) -> float:
    """EV per $1 notional after ~2% fees. side is 'YES' or 'NO'."""
    raw = (fair_prob - yes_price) if side.upper() == "YES" else (yes_price - fair_prob)
    return max(0.0, raw - POLYMARKET_FEE_PCT)


def expected_roi(fair_prob: float, side: str, yes_price: float) -> float:
    """Expected ROI on capital at risk."""
    cost = yes_price if side.upper() == "YES" else (1.0 - yes_price)
    if cost <= 0:
        return 0.0
    return max(0.0, expected_value(fair_prob, side, yes_price) / cost)


def suggest_size(edge: float, bankroll: float, kelly_fraction: float = 0.5,
                 max_pct: float = 0.15) -> float:
    """Advisory Kelly stake. fraction*edge*bankroll, min $1, capped at max_pct
    of bankroll. The agent decides whether to follow this."""
    raw = bankroll * edge * kelly_fraction
    cap = bankroll * max_pct
    return round(min(max(raw, 1.0), cap), 2)
