def test_init_creates_tables(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    import importlib, polymarket_mcp.config as cfg, polymarket_mcp.db as db
    importlib.reload(cfg); importlib.reload(db)
    conn = db.connect("paper")
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"positions", "trades", "market_intelligence", "portfolio_state"} <= tables
    conn.close()


def test_paper_and_live_use_separate_files(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYPILOT_DATA_DIR", str(tmp_path))
    import importlib, polymarket_mcp.config as cfg, polymarket_mcp.db as db
    importlib.reload(cfg); importlib.reload(db)
    assert db.get_db_path("paper") != db.get_db_path("live")
    assert db.get_db_path("paper").name == "paper.db"
