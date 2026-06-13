"""Polypilot MCP server. Tool groups are registered here."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import analytics as _analytics
from . import markets as _markets
from . import order_book as _order_book
from . import risk as _risk
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
                 min_volume: float = 0.0, keyword: str | None = None) -> dict:
    """Discover active, binary Polymarket markets sorted by volume.
    Optionally filter by category (e.g. 'weather','crypto','sports'), minimum
    USD volume, or a keyword in the question. Returns a list of market dicts."""
    found = _markets.fetch_active_markets(limit=max(limit, 50))
    out = []
    for m in found:
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
    for m in _markets.fetch_active_markets(limit=200):
        if m.condition_id == market_id:
            return m.to_dict()
    return {"error": f"market {market_id} not found among active markets"}


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


def main() -> None:
    """Entry point for the `polypilot-mcp` script and `python -m polymarket_mcp`."""
    mcp.run()


if __name__ == "__main__":
    main()
