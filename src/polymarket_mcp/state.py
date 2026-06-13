"""Data-access layer: portfolio, positions, trades, market intelligence."""
from __future__ import annotations

import json
import time

from . import config, db


def _ensure_portfolio(conn, mode: str) -> None:
    row = conn.execute("SELECT id FROM portfolio_state WHERE id = 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO portfolio_state (id, mode, starting_balance, cash) "
            "VALUES (1, ?, ?, ?)",
            (mode, config.STARTING_BALANCE_USD, config.STARTING_BALANCE_USD),
        )
        conn.commit()


def get_portfolio(mode: str | None = None) -> dict:
    mode = mode or config.MODE
    conn = db.connect(mode)
    try:
        _ensure_portfolio(conn, mode)
        ps = conn.execute("SELECT * FROM portfolio_state WHERE id = 1").fetchone()
        open_rows = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(size_usd),0) exp "
            "FROM positions WHERE status='open'").fetchone()
        return {
            "mode": ps["mode"],
            "cash": round(ps["cash"], 2),
            "starting_balance": ps["starting_balance"],
            "realized_pnl": round(ps["realized_pnl"], 2),
            "trade_count": ps["trade_count"],
            "open_positions": open_rows["c"],
            "open_exposure_usd": round(open_rows["exp"], 2),
            "day_realized_pnl": round(ps["day_realized_pnl"], 2),
            "breaker_tripped": bool(ps["breaker_tripped"]),
        }
    finally:
        conn.close()


def _roll_day(conn) -> None:
    today = time.strftime("%Y-%m-%d")
    ps = conn.execute("SELECT day_date FROM portfolio_state WHERE id=1").fetchone()
    if ps and ps["day_date"] != today:
        conn.execute(
            "UPDATE portfolio_state SET day_date=?, day_realized_pnl=0 WHERE id=1",
            (today,))


def open_position(mode, *, market_id, token_id, question, category, side,
                  entry_price, size_usd, shares, fair_prob, confidence,
                  thesis, metadata=None) -> int:
    conn = db.connect(mode)
    try:
        _ensure_portfolio(conn, mode)
        cur = conn.execute(
            "INSERT INTO positions (market_id, token_id, question, category, side, "
            "entry_price, size_usd, shares, fair_prob, confidence, thesis, mode, "
            "status, opened_at, metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?, ?)",
            (market_id, token_id, question, category, side, entry_price, size_usd,
             shares, fair_prob, confidence, thesis, mode, time.time(),
             json.dumps(metadata or {})))
        pos_id = cur.lastrowid
        conn.execute(
            "INSERT INTO trades (position_id, market_id, side, action, size_usd, "
            "price, shares, mode, created_at) VALUES (?,?,?, 'buy', ?,?,?,?,?)",
            (pos_id, market_id, side, size_usd, entry_price, shares, mode, time.time()))
        conn.execute(
            "UPDATE portfolio_state SET cash = cash - ?, trade_count = trade_count + 1 "
            "WHERE id = 1", (size_usd,))
        conn.commit()
        return pos_id
    finally:
        conn.close()


def close_position(mode, position_id: int, *, exit_price: float, payout: float) -> dict:
    conn = db.connect(mode)
    try:
        _ensure_portfolio(conn, mode)
        _roll_day(conn)
        pos = conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        if pos is None or pos["status"] != "open":
            return {"error": f"position {position_id} not open"}
        pnl = payout - pos["size_usd"]
        conn.execute(
            "UPDATE positions SET status='closed', closed_at=?, exit_price=?, "
            "realized_pnl=? WHERE id=?",
            (time.time(), exit_price, pnl, position_id))
        conn.execute(
            "INSERT INTO trades (position_id, market_id, side, action, size_usd, "
            "price, shares, mode, created_at) VALUES (?,?,?, 'sell', ?,?,?,?,?)",
            (position_id, pos["market_id"], pos["side"], payout, exit_price,
             pos["shares"], mode, time.time()))
        conn.execute(
            "UPDATE portfolio_state SET cash = cash + ?, realized_pnl = realized_pnl + ?, "
            "day_realized_pnl = day_realized_pnl + ? WHERE id = 1",
            (payout, pnl, pnl))
        conn.commit()
        return {"position_id": position_id, "realized_pnl": round(pnl, 2)}
    finally:
        conn.close()


def get_positions(mode: str | None = None, status: str = "open") -> list[dict]:
    conn = db.connect(mode or config.MODE)
    try:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status=? ORDER BY opened_at DESC",
            (status,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_trade_history(mode: str | None = None, limit: int = 50) -> list[dict]:
    conn = db.connect(mode or config.MODE)
    try:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def record_evaluation(mode, *, market_id, question, category, keywords, decision,
                      side="", confidence=0.0, fair_prob=None, yes_price=0.0,
                      thesis="") -> int:
    conn = db.connect(mode)
    try:
        cur = conn.execute(
            "INSERT INTO market_intelligence (market_id, question, category, keywords, "
            "decision, side, confidence, fair_prob, yes_price, thesis, evaluated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (market_id, question, category, keywords, decision, side, confidence,
             fair_prob, yes_price, thesis, time.time()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_similar_markets(mode, text: str, limit: int = 5) -> list[dict]:
    """Keyword-overlap lookup over past evaluations. Mirrors PolyDegen's
    similar-market intelligence: rank by count of shared tokens."""
    tokens = {t for t in text.lower().split() if len(t) > 2}
    conn = db.connect(mode or config.MODE)
    try:
        rows = conn.execute("SELECT * FROM market_intelligence ORDER BY evaluated_at DESC "
                            "LIMIT 500").fetchall()
    finally:
        conn.close()
    scored = []
    for r in rows:
        kw = set((r["keywords"] or "").lower().split()) | set((r["question"] or "").lower().split())
        overlap = len(tokens & kw)
        if overlap:
            scored.append((overlap, dict(r)))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in scored[:limit]]
