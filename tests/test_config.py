import importlib


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import polymarket_mcp.config as cfg
    return importlib.reload(cfg)


def test_defaults(monkeypatch):
    for var in ["POLYPILOT_MODE", "POLYPILOT_STARTING_BALANCE_USD",
                "POLYPILOT_KILL_SWITCH_DAILY_USD", "POLYPILOT_KILL_SWITCH_TOTAL_USD"]:
        monkeypatch.delenv(var, raising=False)
    cfg = _reload(monkeypatch)
    assert cfg.MODE == "paper"
    assert cfg.STARTING_BALANCE_USD == 200.0
    assert cfg.KILL_SWITCH_DAILY_USD == 50.0
    assert cfg.KILL_SWITCH_TOTAL_USD == 100.0


def test_live_mode_and_overrides(monkeypatch):
    cfg = _reload(monkeypatch, POLYPILOT_MODE="live",
                  POLYPILOT_KILL_SWITCH_DAILY_USD="25.5")
    assert cfg.MODE == "live"
    assert cfg.KILL_SWITCH_DAILY_USD == 25.5


def test_invalid_mode_falls_back_to_paper(monkeypatch):
    cfg = _reload(monkeypatch, POLYPILOT_MODE="garbage")
    assert cfg.MODE == "paper"


def test_malformed_signature_type_falls_back_to_zero(monkeypatch):
    cfg = _reload(monkeypatch, POLYMARKET_SIGNATURE_TYPE="bad")
    assert cfg.POLYMARKET_SIGNATURE_TYPE == 0
