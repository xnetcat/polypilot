# tests/test_analytics_extra.py
"""Breadth coverage for advisory trading math in analytics.py.

These exercise the NO side, ROI cost-basis guards, and the Kelly sizing
cap/floor/scaling behaviour. They assert the CURRENT correct behaviour and
must pass against the source as-is.
"""
import polymarket_mcp.analytics as a

# V2 price-dependent fee at the default rate (0.04, exp 1): fee = 0.04*p*(1-p).


# --- expected_value: NO side --------------------------------------------------

def test_expected_value_no_at_several_prices():
    # NO raw = yes_price - fair_prob, then minus the V2 fee at the yes_price.
    # yes 0.60, fair 0.30 -> raw 0.30 - 0.04*(0.6*0.4)=0.0096 -> 0.2904
    assert round(a.expected_value(0.30, "NO", 0.60), 4) == 0.2904
    # yes 0.50, fair 0.45 -> raw 0.05 - 0.04*0.25=0.01 -> 0.04
    assert round(a.expected_value(0.45, "NO", 0.50), 4) == 0.04
    # yes 0.80, fair 0.20 -> raw 0.60 - 0.04*(0.8*0.2)=0.0064 -> 0.5936
    assert round(a.expected_value(0.20, "NO", 0.80), 4) == 0.5936


def test_expected_value_no_floors_at_zero_when_negative():
    # yes 0.40, fair 0.50 -> raw -0.10 minus fee -> floored to 0.0
    assert a.expected_value(0.50, "NO", 0.40) == 0.0
    # A tiny positive raw below the (near-0.5) fee floors to 0.0:
    # yes 0.505, fair 0.50 -> raw 0.005 - ~0.01 fee -> 0.0
    assert a.expected_value(0.50, "NO", 0.505) == 0.0


def test_expected_value_fee_free_market_keeps_raw_edge():
    # fee_rate=0 (fees disabled on the market) -> no haircut at all.
    assert round(a.expected_value(0.30, "NO", 0.60, fee_rate=0.0), 4) == 0.30


def test_effective_fee_is_parabolic_and_symmetric():
    # Max at 0.5, ~0 at the extremes, symmetric in price.
    assert round(a.effective_fee(0.5), 6) == round(0.04 * 0.25, 6)   # peak
    assert round(a.effective_fee(0.95), 6) == round(a.effective_fee(0.05), 6)  # symmetric
    assert a.effective_fee(0.95) < a.effective_fee(0.5)              # favorite pays less
    assert a.effective_fee(0.5, fee_rate=0.0) == 0.0                # disabled


def test_expected_value_side_is_case_insensitive():
    # 'yes'/'no' (lowercase) must behave identically to 'YES'/'NO'.
    assert a.expected_value(0.60, "yes", 0.45) == a.expected_value(0.60, "YES", 0.45)
    assert a.expected_value(0.30, "no", 0.60) == a.expected_value(0.30, "NO", 0.60)
    # Mixed case too.
    assert a.expected_value(0.30, "No", 0.60) == a.expected_value(0.30, "NO", 0.60)


# --- expected_roi -------------------------------------------------------------

def test_expected_roi_no_uses_one_minus_yes_price_cost_basis():
    # NO cost basis = (1 - yes_price). yes_price 0.60 -> cost 0.40.
    # EV(fair 0.30, NO, 0.60) = 0.2904 -> ROI = 0.2904 / 0.40 = 0.726
    ev = a.expected_value(0.30, "NO", 0.60)
    assert round(a.expected_roi(0.30, "NO", 0.60), 4) == round(ev / 0.40, 4)
    assert round(a.expected_roi(0.30, "NO", 0.60), 4) == 0.726


def test_expected_roi_no_cost_guard_returns_zero_at_yes_price_one():
    # NO at yes_price 1.0 -> cost = 1 - 1 = 0 -> cost<=0 guard returns 0.0
    assert a.expected_roi(0.50, "NO", 1.0) == 0.0


def test_expected_roi_yes_cost_guard_returns_zero_at_yes_price_zero():
    # YES cost = yes_price; at 0.0 the cost<=0 guard also returns 0.0.
    assert a.expected_roi(0.50, "YES", 0.0) == 0.0


# --- suggest_size -------------------------------------------------------------

def test_suggest_size_large_edge_hits_max_pct_cap():
    # raw = 100 * 0.9 * 0.5 = 45, but cap = 100 * 0.15 = 15 -> capped at 15.0
    assert a.suggest_size(edge=0.9, bankroll=100.0) == 15.0


def test_suggest_size_custom_max_pct_cap():
    # cap = 100 * 0.25 = 25; raw = 100 * 0.9 * 0.5 = 45 -> capped at 25.0
    assert a.suggest_size(edge=0.9, bankroll=100.0, max_pct=0.25) == 25.0


def test_suggest_size_tiny_edge_floors_at_one_dollar():
    # raw = 100 * 0.0001 * 0.5 = 0.005 -> floored to the $1.0 minimum
    assert a.suggest_size(edge=0.0001, bankroll=100.0) == 1.0


def test_suggest_size_kelly_fraction_scales_linearly():
    # Below both cap and floor, stake is linear in kelly_fraction.
    full = a.suggest_size(edge=0.1, bankroll=100.0, kelly_fraction=1.0)   # 10.0
    half = a.suggest_size(edge=0.1, bankroll=100.0, kelly_fraction=0.5)   # 5.0
    quarter = a.suggest_size(edge=0.1, bankroll=100.0, kelly_fraction=0.25)  # 2.5
    assert full == 10.0
    assert half == 5.0
    assert quarter == 2.5
    assert round(half, 6) == round(full * 0.5, 6)
    assert round(quarter, 6) == round(full * 0.25, 6)


def test_suggest_size_rounds_to_two_decimals():
    # raw = 33.33 * 0.13 * 0.5 = 2.166...; cap = 33.33 * 0.15 = 5.0 (not binding)
    # -> rounded to 2dp = 2.17
    assert a.suggest_size(edge=0.13, bankroll=33.33) == 2.17


# --- sizing_plan: order-minimum awareness -------------------------------------

def test_sizing_plan_flags_unplaceable_limit_on_tiny_bankroll():
    # $11.69 bankroll, edge 0.04 -> half-Kelly ~ $0.23 -> floored to $1. At price
    # 0.84 a LIMIT needs 5 shares = $4.20, so the limit is unplaceable; a market
    # order (min ~$1) is fine.
    plan = a.sizing_plan(edge=0.04, bankroll=11.69, price=0.84)
    assert plan["min_limit_usd"] == 4.2
    assert plan["limit_placeable_at_suggested"] is False
    assert plan["market_placeable_at_suggested"] is True
    assert "market" in plan["note"].lower()


def test_sizing_plan_placeable_when_stake_clears_floor():
    # Big edge + bankroll -> suggested clears the 5-share floor at price 0.50.
    plan = a.sizing_plan(edge=0.5, bankroll=100.0, price=0.50)
    assert plan["limit_placeable_at_suggested"] is True
    assert "note" not in plan


def test_sizing_plan_without_price_is_bare_suggestion():
    plan = a.sizing_plan(edge=0.1, bankroll=100.0)
    assert plan["suggested_usd"] == 5.0
    assert "min_limit_usd" not in plan
