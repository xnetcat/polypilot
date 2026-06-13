"""Polypilot MCP server. Tool groups are registered here."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import analytics as _analytics
from . import execution as _execution
from . import markets as _markets
from . import notifications as _notifications
from . import order_book as _order_book
from . import pacing as _pacing
from . import risk as _risk
from . import state as _state
from .research import crypto as _crypto
from .research import economic as _economic
from .research import news as _news
from .research import sports as _sports
from .research import weather as _weather

mcp = FastMCP("polypilot")


@mcp.tool()
def ping() -> dict:
    """Health check. Returns service status."""
    return {"status": "ok", "service": "polypilot-mcp"}


# --- Market data tools ---


@mcp.tool()
def list_markets(limit: int = 50, category: str | None = None,
                 min_volume: float = 0.0, keyword: str | None = None,
                 binary_only: bool = False) -> dict:
    """Discover active Polymarket markets sorted by volume — binary (YES/NO) AND
    multi-outcome by default. Each market dict carries `is_binary` plus an
    `outcomes` list ([{outcome, token_id, price}, ...]) so multi-outcome data
    flows through automatically. Optionally filter by category (e.g.
    'weather','crypto','sports'), minimum USD volume, or a keyword in the
    question. Set `binary_only=True` to return only YES/NO markets. Returns a
    list of market dicts."""
    found = _markets.fetch_active_markets(limit=max(limit, 50))
    out = []
    for m in found:
        if binary_only and not m.is_binary:
            continue
        if category and m.category != category:
            continue
        if m.volume < min_volume:
            continue
        if keyword and keyword.lower() not in m.question.lower():
            continue
        out.append(m.to_dict())
    return {"count": len(out[:limit]), "markets": out[:limit]}


@mcp.tool()
def get_market(market_id: str) -> dict:
    """Fetch one market's full detail by condition id. Returns the market dict
    or {'error': ...} if not found."""
    market = _markets.fetch_market(market_id)
    if market is None:
        return {"error": f"market {market_id} not found"}
    return market.to_dict()


@mcp.tool()
def get_order_book(token_id: str) -> dict:
    """Live order book summary for a CLOB token id (best bid/ask, spread,
    midpoint, imbalance, top 10 levels). Get token ids from get_market()."""
    return _order_book.fetch_order_book(token_id)


@mcp.tool()
def get_price_history(token_id: str, interval: str = "1d") -> dict:
    """Price-history summary for a CLOB token id over an interval
    (1m,1h,6h,1d,1w,max). Returns first/last/high/low/change."""
    return _order_book.fetch_price_history(token_id, interval)


# --- Analytics tools ---


@mcp.tool()
def compute_edge(fair_prob: float, side: str, yes_price: float) -> dict:
    """ADVISORY. Given your probability estimate (fair_prob), a side ('YES'/'NO'),
    and the market YES price, returns expected value and ROI per $1 after ~2% fees.
    This does not gate trades — it is a calculator for your own judgment."""
    return {
        "expected_value": round(_analytics.expected_value(fair_prob, side, yes_price), 4),
        "expected_roi": round(_analytics.expected_roi(fair_prob, side, yes_price), 4),
        "fair_prob": fair_prob, "side": side.upper(), "yes_price": yes_price,
    }


@mcp.tool()
def suggest_size(edge: float, bankroll: float, kelly_fraction: float = 0.5) -> dict:
    """ADVISORY half-Kelly stake suggestion. You choose whether to use it,
    size up, or size down."""
    return {"suggested_usd": _analytics.suggest_size(edge, bankroll, kelly_fraction)}


# --- Safety ---


@mcp.tool()
def get_risk_status() -> dict:
    """Current safety state: cash, realized PnL (total + today), open exposure,
    kill-switch limits, and whether the breaker is tripped. Call this FIRST each
    cycle — if breaker_tripped is true, STOP trading and report."""
    return _risk.risk_status()


# --- Research tools ---


@mcp.tool()
def get_weather_forecast(question: str) -> dict:
    """Structured weather forecast for a weather market question (parses the city,
    date, and temperature target; returns forecast high/low in °C and
    uncertainty). Use this to form a thesis on weather markets."""
    return _weather.fetch_weather_forecast(question)


@mcp.tool()
def get_crypto(question: str) -> dict:
    """Crypto spot price, 24h change, recent history, and (if the `ml` extra is
    installed) a Chronos direction prediction. Parses the asset from the
    question. Use to form a thesis on crypto markets."""
    return _crypto.fetch_crypto(question)


@mcp.tool()
def get_sports(question: str) -> dict:
    """ESPN-backed data for a sports market: detected sport, teams, game status,
    and live/last score. Use to form a thesis on sports markets."""
    return _sports.fetch_sports(question)


@mcp.tool()
def get_economic_data(question: str) -> dict:
    """FRED-backed official economic data (inflation, GDP, unemployment, rates)
    matched to the question. Requires FRED_API_KEY. Use for economy/finance
    markets."""
    return _economic.fetch_economic_data(question)


@mcp.tool()
def get_news(topic: str, limit: int = 20) -> dict:
    """Recent news headlines (RSS, plus NewsAPI if configured) matching a topic.
    Use to form or re-check a thesis on news-driven markets."""
    return _news.fetch_news(topic, limit=limit)


# --- Execution tools ---


@mcp.tool()
def place_order(market_id: str, token_id: str, question: str, category: str,
                side: str, size_usd: float, fair_prob: float | None = None,
                confidence: float | None = None, thesis: str = "",
                limit_price: float | None = None,
                order_type: str = "market") -> dict:
    """Buy `side` on a market for size_usd. For BINARY markets `side` is 'YES' or
    'NO'; for MULTI-OUTCOME markets (is_binary=false) `side` is the outcome label
    (e.g. "Lakers") whose token you're buying. Records your thesis, confidence,
    and fair_prob with the position. Enforces the kill-switch and refuses if the
    breaker is tripped, cash is insufficient, or you already hold an open
    position in this market. Returns fill details or a rejection reason. Get
    token_id from get_market(): pass the `token_id` whose `outcome` matches the
    side you're buying (from get_market's `outcomes`/`tokens` list) — settlement
    keys off that token. `order_type` is 'market' (default; FAK in live,
    slippage-walked in paper) or 'limit' (GTC resting order at `limit_price`)."""
    return _execution.place_order(market_id=market_id, token_id=token_id,
        question=question, category=category, side=side, size_usd=size_usd,
        fair_prob=fair_prob, confidence=confidence, thesis=thesis,
        limit_price=limit_price, order_type=order_type)


@mcp.tool()
def reconcile_positions() -> dict:
    """Settle any open positions whose markets have resolved (price → 0 or 1),
    crediting payouts and updating realized PnL. Run at the start of each cycle."""
    return _execution.reconcile_positions()


@mcp.tool()
def close_position(position_id: int) -> dict:
    """Sell an open position now at the current best bid. Use to recycle capital
    out of near-resolved positions when you have a clearly better edge — NOT as a
    stop-loss."""
    return _execution.close_position(position_id=position_id)


# --- Live trading ops (read-only checks; no live trade is placed here) ---


@mcp.tool()
def get_live_balance() -> dict:
    """LIVE: fetch the on-chain USDC (collateral) balance from the CLOB. Returns
    {'balance_usd': float} or {'error': ...} when no key is set or the call
    fails. Read-only."""
    return _execution.get_live_balance()


@mcp.tool()
def sync_live_positions() -> dict:
    """LIVE: compare on-chain positions to locally-tracked open positions and
    REPORT the diff (on_chain_only / local_only / matched / in_sync) WITHOUT
    mutating any state. Use to spot drift between the local ledger and the
    chain. Read-only."""
    return _execution.sync_live_positions()


@mcp.tool()
def preflight_live() -> dict:
    """LIVE: pre-flight checklist before live trading. Returns {'ok': bool,
    'checks': {...}} covering private key, derivable CLOB creds, positive
    balance, and configured kill-switch limits. Never raises; placing no order."""
    return _execution.preflight_live()


# --- Portfolio + memory tools ---


@mcp.tool()
def get_portfolio() -> dict:
    """Cash, realized/unrealized PnL, open position count, exposure, mode.
    Best-effort marks open positions to market (live book midpoints) to add
    `unrealized_pnl`, `mark_to_market_value`, `total_pnl`, and `marked_positions`
    (how many positions had a usable midpoint). Marking is PER-POSITION: a
    position whose book lacks a usable midpoint is skipped, not fatal — the other
    positions are still marked. If NO position can be marked the unrealized keys
    are omitted and the base portfolio is returned. With zero open positions
    there is NO network call."""
    portfolio = _state.get_portfolio()
    positions = _state.get_positions()
    # No open positions -> nothing to mark; skip all network entirely.
    if not positions:
        return portfolio
    mark_value = 0.0
    cost_basis = 0.0
    marked = 0
    for p in positions:
        try:
            book = _order_book.fetch_order_book(p["token_id"])
            midpoint = book.get("midpoint")
        except Exception:
            midpoint = None  # a failed book fetch just skips this position
        if not midpoint:  # None or 0.0 (no two-sided book) -> can't mark; skip
            continue
        mark_value += p["shares"] * midpoint
        cost_basis += p["size_usd"]
        marked += 1
    # Only enrich when at least one position was markable; otherwise leave the
    # base portfolio untouched (no misleading partial unrealized figures).
    if marked:
        unrealized = mark_value - cost_basis
        portfolio["unrealized_pnl"] = round(unrealized, 2)
        portfolio["mark_to_market_value"] = round(mark_value, 2)
        portfolio["total_pnl"] = round(portfolio["realized_pnl"] + unrealized, 2)
        portfolio["marked_positions"] = marked
    return portfolio


@mcp.tool()
def get_positions(status: str = "open") -> dict:
    """Open (or 'closed') positions with entry price, size, shares, and the
    stored thesis/confidence/fair_prob you saved at entry."""
    return {"positions": _state.get_positions(status=status)}


@mcp.tool()
def get_trade_history(limit: int = 50) -> dict:
    """Recent buy/sell trades with prices and outcomes."""
    return {"trades": _state.get_trade_history(limit=limit)}


@mcp.tool()
def get_performance_stats(category: str | None = None) -> dict:
    """Track-record stats from RESOLVED evaluations: win rate, total/avg PnL, and
    ROI, broken down by category plus an overall bucket. Optionally restrict to a
    single `category`. Use to learn which categories you trade well."""
    return _state.get_performance_stats(category=category)


@mcp.tool()
def get_session_report() -> dict:
    """One-shot summary of the current session: portfolio (with mark-to-market),
    risk/kill-switch status, performance stats, the 10 most recent trades, and
    the open-position count. Use for an end-of-cycle or status snapshot."""
    return {
        "portfolio": get_portfolio(),
        "risk": _risk.risk_status(),
        "performance": _state.get_performance_stats(),
        # Wrapped as {"trades": [...]} to match the standalone get_trade_history
        # tool's shape for consistency.
        "recent_trades": {"trades": _state.get_trade_history(limit=10)},
        "open_positions": len(_state.get_positions(status="open")),
    }


@mcp.tool()
def record_thesis(position_id: int, thesis: str, confidence: float,
                  fair_prob: float) -> dict:
    """Attach/refresh your written thesis, confidence, and fair probability to an
    open position so you can re-check it against new data next cycle."""
    ok = _state.update_thesis(_state.config.MODE, position_id, thesis,
                              confidence, fair_prob)
    return {"status": "ok" if ok else "not_found", "position_id": position_id}


@mcp.tool()
def get_similar_markets(keywords: str, limit: int = 5) -> dict:
    """Your historical track record on past markets matching these keywords —
    what you decided and how it resolved. Use to screen candidates."""
    return {"similar": _state.get_similar_markets(_state.config.MODE, keywords, limit)}


@mcp.tool()
def record_evaluation(market_id: str, question: str, category: str,
                      keywords: str, decision: str, side: str = "",
                      confidence: float = 0.0, fair_prob: float | None = None,
                      yes_price: float = 0.0, thesis: str = "",
                      stake_usd: float = 0.0) -> dict:
    """Log the view you formed on a market this cycle — for EVERY market you
    evaluated, whether you traded it or skipped it. `decision` is one of
    'buy_yes' | 'buy_no' | 'skip'. Pass concise lowercased `keywords`, your
    `thesis`, `confidence`, `fair_prob`, and the market `yes_price`. Pass
    `stake_usd` — the $ you committed to the trade (0 for skips/unsized) — so the
    stake-based ROI in get_performance_stats has a denominator once the market
    resolves. This is what builds the track record that get_similar_markets reads
    next cycle; outcomes are backfilled automatically when the market resolves."""
    row_id = _state.record_evaluation(_state.config.MODE, market_id=market_id,
        question=question, category=category, keywords=keywords,
        decision=decision, side=side, confidence=confidence, fair_prob=fair_prob,
        yes_price=yes_price, thesis=thesis, stake_usd=stake_usd)
    return {"status": "ok", "id": row_id}


# --- Notifications ---


@mcp.tool()
def send_notification(event: str, message: str) -> dict:
    """Send a best-effort outbound notification (webhook and/or Telegram) tagged
    with `event`. STRICT NO-OP when no channel is configured: returns
    {'sent': False, 'reason': 'no channel configured'} and makes NO network call.
    Never raises — transport errors are captured per-channel. Use to alert on
    fills, kill-switch trips, or end-of-cycle summaries."""
    return _notifications.notify(event, message)


# --- Pacing ---


@mcp.tool()
def wait_for_next_cycle(min_seconds: int = 300) -> dict:
    """Pause between trading cycles. Sleeps server-side for ~min_seconds but
    returns early if a held position nears resolution so you can settle it.
    Call this at the end of each cycle, then start the next cycle."""
    return _pacing.wait_for_next_cycle(min_seconds=min_seconds)


def main() -> None:
    """Entry point for the `polypilot-mcp` script and `python -m polymarket_mcp`."""
    mcp.run()


if __name__ == "__main__":
    main()
