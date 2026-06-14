"""Advisory trading math: expected value, ROI, Kelly sizing.
These are TOOLS the agent may call to sanity-check its own numbers — NOT gates."""
from __future__ import annotations

# Polymarket CLOB V2 charges a PRICE-DEPENDENT platform fee, not a flat rate:
#   fee_per_share = rate * (price * (1 - price)) ** exponent
# a parabola that is maximal at price 0.5 and ~0 at the extremes — so favorites
# (e.g. a weather-NO at 0.95) pay almost nothing, while coin-flips near 0.5 pay
# the most. Many markets have fees DISABLED (rate 0). Pass a market's real
# fee_rate / fee_exponent (surfaced on the market dict from Gamma's feeSchedule)
# for an exact figure; the defaults below are a conservative typical fee used
# when the real schedule isn't supplied.
DEFAULT_FEE_RATE = 0.04
DEFAULT_FEE_EXPONENT = 1.0


def effective_fee(price: float, fee_rate: float = DEFAULT_FEE_RATE,
                  fee_exponent: float = DEFAULT_FEE_EXPONENT) -> float:
    """Per-share V2 platform fee at a fill ``price`` (as a fraction of the $1
    payoff). ``rate * (price*(1-price))**exponent``; symmetric in price (YES and
    NO give the same fee), and 0 when ``fee_rate`` <= 0."""
    if fee_rate <= 0:
        return 0.0
    p = min(max(price, 0.0), 1.0)
    try:
        return fee_rate * (p * (1.0 - p)) ** fee_exponent
    except (ValueError, OverflowError):
        return 0.0


def expected_value(fair_prob: float, side: str, yes_price: float,
                   fee_rate: float = DEFAULT_FEE_RATE,
                   fee_exponent: float = DEFAULT_FEE_EXPONENT) -> float:
    """EV per $1 notional after the V2 platform fee. side is 'YES' or 'NO'."""
    raw = (fair_prob - yes_price) if side.upper() == "YES" else (yes_price - fair_prob)
    return max(0.0, raw - effective_fee(yes_price, fee_rate, fee_exponent))


def expected_roi(fair_prob: float, side: str, yes_price: float,
                 fee_rate: float = DEFAULT_FEE_RATE,
                 fee_exponent: float = DEFAULT_FEE_EXPONENT) -> float:
    """Expected ROI on capital at risk."""
    cost = yes_price if side.upper() == "YES" else (1.0 - yes_price)
    if cost <= 0:
        return 0.0
    return max(0.0, expected_value(fair_prob, side, yes_price, fee_rate,
                                   fee_exponent) / cost)


def suggest_size(edge: float, bankroll: float, kelly_fraction: float = 0.5,
                 max_pct: float = 0.15) -> float:
    """Advisory Kelly stake. fraction*edge*bankroll, min $1, capped at max_pct
    of bankroll. The agent decides whether to follow this."""
    raw = bankroll * edge * kelly_fraction
    cap = bankroll * max_pct
    return round(min(max(raw, 1.0), cap), 2)


def sizing_plan(edge: float, bankroll: float, price: float | None = None,
                kelly_fraction: float = 0.5, max_pct: float = 0.15,
                min_order_shares: float = 5.0,
                min_market_usd: float = 1.0) -> dict:
    """Advisory stake PLUS placeability against Polymarket's order minimums.

    Polymarket rejects a LIMIT order below ``min_order_shares`` (5) shares; a
    MARKET (FAK) order only needs ~$``min_market_usd`` (1) of notional. At a high
    ``price`` the 5-share floor can force a limit far above the half-Kelly stake
    (5 shares @ 0.84 = $4.20), so this flags when a limit would be unplaceable
    and points to a market order. Returns the suggested stake, the limit floor,
    and whether each order type is placeable at ``price``."""
    suggested = suggest_size(edge, bankroll, kelly_fraction, max_pct)
    out: dict = {
        "suggested_usd": suggested,
        "cap_usd": round(bankroll * max_pct, 2),
    }
    if price is not None and price > 0:
        min_limit_usd = round(min_order_shares * price, 2)
        out["price"] = price
        out["min_limit_usd"] = min_limit_usd
        out["min_market_usd"] = min_market_usd
        out["limit_placeable_at_suggested"] = suggested >= min_limit_usd
        out["market_placeable_at_suggested"] = suggested >= min_market_usd
        if suggested < min_limit_usd:
            out["note"] = (
                f"half-Kelly ${suggested} is below the {min_order_shares:g}-share "
                f"limit floor (${min_limit_usd} at price {price}); place a MARKET "
                f"order (min ~${min_market_usd:g}) or raise size to ${min_limit_usd}."
            )
    return out
