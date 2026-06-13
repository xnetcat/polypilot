"""SQLite persistence. Separate DB files for paper vs live."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

_INITIALIZED: set[str] = set()

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL DEFAULT '',
    question TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    size_usd REAL NOT NULL,
    shares REAL NOT NULL,
    fair_prob REAL,
    confidence REAL,
    thesis TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'paper',
    status TEXT NOT NULL DEFAULT 'open',
    opened_at REAL NOT NULL,
    closed_at REAL,
    exit_price REAL,
    realized_pnl REAL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER REFERENCES positions(id),
    market_id TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,              -- 'buy' | 'sell'
    size_usd REAL NOT NULL,
    price REAL NOT NULL,
    shares REAL NOT NULL,
    order_id TEXT,
    mode TEXT NOT NULL DEFAULT 'paper',
    status TEXT NOT NULL DEFAULT 'filled',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS market_intelligence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    question TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL,            -- 'buy_yes' | 'buy_no' | 'skip'
    side TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    fair_prob REAL,
    yes_price REAL NOT NULL DEFAULT 0,
    thesis TEXT NOT NULL DEFAULT '',
    outcome_side TEXT NOT NULL DEFAULT '',
    resolution_price REAL,
    pnl REAL,
    resolved INTEGER NOT NULL DEFAULT 0,
    evaluated_at REAL NOT NULL,
    resolved_at REAL
);

CREATE TABLE IF NOT EXISTS portfolio_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL,
    starting_balance REAL NOT NULL,
    cash REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    trade_count INTEGER NOT NULL DEFAULT 0,
    day_date TEXT NOT NULL DEFAULT '',
    day_realized_pnl REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);
CREATE INDEX IF NOT EXISTS idx_intel_keywords ON market_intelligence(keywords);
CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_open_market
    ON positions(market_id) WHERE status = 'open';
"""


def get_data_dir() -> Path:
    if config.DATA_DIR:
        return Path(config.DATA_DIR).expanduser()
    return Path.home() / ".local" / "share" / "polypilot"


def get_db_path(mode: str | None = None) -> Path:
    m = (mode or config.MODE).strip().lower()
    if m not in {"paper", "live"}:
        m = "paper"
    return get_data_dir() / f"{m}.db"


def init_db(mode: str | None = None) -> Path:
    db_path = get_db_path(mode)
    key = str(db_path)
    if key in _INITIALIZED and db_path.exists():
        return db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    _INITIALIZED.add(key)
    return db_path


def connect(mode: str | None = None) -> sqlite3.Connection:
    init_db(mode)
    conn = sqlite3.connect(get_db_path(mode))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn
