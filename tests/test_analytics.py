import polymarket_mcp.analytics as a


def test_expected_value_yes():
    # fair 0.60, price 0.45, YES -> raw 0.15 - V2 fee 0.04*(0.45*0.55)=0.0099 = 0.1401
    assert round(a.expected_value(0.60, "YES", 0.45), 4) == 0.1401


def test_expected_value_no():
    # fair 0.30, price 0.45 -> NO raw 0.45-0.30=0.15 - 0.0099 = 0.1401
    assert round(a.expected_value(0.30, "NO", 0.45), 4) == 0.1401


def test_expected_value_floors_at_zero():
    assert a.expected_value(0.50, "YES", 0.90) == 0.0


def test_roi_is_ev_over_cost():
    ev = a.expected_value(0.60, "YES", 0.45)
    assert round(a.expected_roi(0.60, "YES", 0.45), 4) == round(ev / 0.45, 4)


def test_suggest_size_half_kelly_capped():
    # edge 0.13, bankroll 100, half-kelly -> 100*0.13*0.5 = 6.5, under cap
    assert a.suggest_size(edge=0.13, bankroll=100.0, kelly_fraction=0.5) == 6.5


def test_suggest_size_minimum_one_dollar():
    assert a.suggest_size(edge=0.001, bankroll=100.0) == 1.0
