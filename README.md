# Polypilot

Autonomous Polymarket trading where **the agent is the trader**. Polypilot is a
[Claude Code](https://docs.claude.com/en/docs/claude-code) plugin that pairs a
skill (`/polypilot`) with a Python MCP server (`polypilot-mcp`). The agent
reasons, forms theses, and decides every trade; the deterministic MCP tools
supply market and research data, do the math the agent asks for, execute orders,
and remember outcomes.

> **Disclaimer:** This project is for entertainment and educational purposes
> only. It is **not financial advice**. Prediction market trading carries
> significant risk — you can lose money. Never trade with funds you cannot
> afford to lose. Use at your own risk.

---

## What makes it different from a trading bot

A traditional bot hardcodes the strategy: fixed signal rules, edge thresholds,
confidence minimums, and sizing formulas decide the trades, and an LLM (if any)
is a called sub-component. Polypilot inverts that. **The agent is the brain.** It
discovers markets, researches them, estimates a fair probability, writes a
thesis, and chooses whether and how much to bet — all in judgment, not in code.

The MCP server is pure capability plus exactly **one** hard guardrail: a
cumulative-loss kill-switch enforced inside `place_order`, which the agent cannot
trade around. There are no hardcoded strategy thresholds, no per-trade size caps,
and no concentration limits. Sizing, timing, and selectivity are all the agent's
call.

## Architecture

Two halves ship in one plugin:

- **Skill (`/polypilot`)** — the thought-process loop. It encodes a continuous
  in-session cycle (`reconcile → review → discover → screen → research → decide →
  trade → wait`) and ships per-category playbooks (weather, crypto, reading order
  books, exit doctrine) as *guidance*, never as gates. The agent runs this loop
  until you stop it or the kill-switch trips.
- **MCP server (`polypilot-mcp`)** — a [FastMCP](https://github.com/modelcontextprotocol/python-sdk)
  Python server exposing the tools below, backed by **SQLite** state (positions,
  trades, market intelligence, portfolio) with separate database files for paper
  and live. The kill-switch lives here.

## Install as a Claude Code plugin

Install the plugin (skill + bundled MCP server). The plugin's `.mcp.json`
launches the server via `uv run`, so installation wires everything up
automatically — you just need [`uv`](https://docs.astral.sh/uv/) and Python 3.11+.

Once installed, start the trading loop in any Claude Code session:

```
/polypilot
```

This launches the in-session loop. At startup the agent reports the active mode,
cash, and kill-switch limits, then begins cycling: each cycle it checks safety,
settles resolved positions, reviews held positions against their stored theses,
discovers and screens candidates, researches them, forms an explicit
fair-probability estimate, decides (and often skips), trades, reports a summary,
and waits before the next cycle. It keeps going until you stop it or the breaker
trips. In **live** mode it prints a one-time real-money banner at startup — there
is no per-trade confirmation, by design (that would break autonomy).

## Paper vs live mode

| Mode | How to enable | Behavior |
|------|---------------|----------|
| **paper** (default) | nothing (or `POLYPILOT_MODE=paper`) | Simulated fills against the live order book. No real money. |
| **live** (opt-in) | `POLYPILOT_MODE=live` + `POLYMARKET_PRIVATE_KEY` (install the `trade` extra) | Real orders via the Polymarket CLOB. |

Paper and live use **separate SQLite database files** (`paper.db` / `live.db`),
so paper experiments never touch live state.

## The kill-switch (the only hard guardrail)

A single cumulative-loss circuit breaker is the one rule enforced in code:

- It trips when **daily** realized loss reaches `POLYPILOT_KILL_SWITCH_DAILY_USD`
  (default **$50**) **or** **total** cumulative realized loss reaches
  `POLYPILOT_KILL_SWITCH_TOTAL_USD` (default **$100**).
- It is enforced **inside `place_order`** — when tripped, orders are refused, and
  the agent halts the loop and reports. The agent cannot override it.
- `get_risk_status()` surfaces the current state (PnL vs. limits, breaker state).

Everything else — strategy, edge bar, position sizing, concentration, and
timing — is the agent's judgment, not hardcoded.

## Tool surface (22 tools)

Grouped by purpose. The agent calls what it needs; all tools return plain JSON.

**Markets**
- `list_markets` — discover active binary markets (filter by category, min volume, keyword), sorted by volume.
- `get_market` — full detail for one market by condition id.
- `get_order_book` — live order book summary for a CLOB token (best bid/ask, spread, midpoint, imbalance, top levels).
- `get_price_history` — price-history summary for a token over an interval (first/last/high/low/change).

**Research**
- `get_weather_forecast` — structured forecast (high/low °C, uncertainty) parsed from a weather market question (Open-Meteo + weather.gov).
- `get_crypto` — spot price, 24h change, recent history, and an optional Chronos direction prediction (behind the `ml` extra).
- `get_sports` — ESPN-backed sport/teams/status/score for a sports market.
- `get_economic_data` — FRED-backed official economic series (requires `FRED_API_KEY`).
- `get_news` — recent headlines (RSS, plus NewsAPI if configured) matching a topic.

**Analytics** (advisory — calculators, not gates)
- `compute_edge` — expected value and ROI per $1 after ~2% fees, given your fair probability, side, and the market price.
- `suggest_size` — advisory half-Kelly stake suggestion.

**Execution**
- `place_order` — buy a side for a USD amount, storing your thesis/confidence/fair_prob; **enforces the kill-switch** and refuses when tripped or cash is short.
- `reconcile_positions` — settle open positions whose markets resolved, crediting payouts and updating realized PnL.
- `close_position` — sell an open position now at best bid (to recycle capital from near-resolved positions, not as a stop-loss).

**Portfolio / Memory**
- `get_portfolio` — cash, realized/unrealized PnL, open count, exposure, mode.
- `get_positions` — open (or closed) positions with entry, size, shares, and stored thesis.
- `get_trade_history` — recent buy/sell trades with prices and outcomes.
- `record_thesis` — attach/refresh a written thesis, confidence, and fair probability on an open position.
- `get_similar_markets` — your historical track record on past markets matching keywords (powers candidate screening).

**Safety / Pacing**
- `get_risk_status` — cash, PnL vs. kill-switch limits, breaker state, mode. (Call first each cycle.)
- `wait_for_next_cycle` — server-side sleep that paces the loop and may return early if a held position nears resolution.
- `ping` — health check.

## Environment variables

Configure via environment variables or a `.env` file (see `.env.example`). The
`POLYPILOT_` prefix is used for Polypilot-specific settings.

| Variable | Default | Description |
|----------|---------|-------------|
| `POLYPILOT_MODE` | `paper` | `paper` (simulated) or `live` (real money). Invalid values fall back to `paper`. |
| `POLYPILOT_STARTING_BALANCE_USD` | `200` | Paper-trading starting balance. |
| `POLYPILOT_KILL_SWITCH_DAILY_USD` | `50` | Kill-switch: daily cumulative-loss limit. |
| `POLYPILOT_KILL_SWITCH_TOTAL_USD` | `100` | Kill-switch: total cumulative-loss limit. |
| `POLYPILOT_DATA_DIR` | _(platform default)_ | Override the SQLite storage directory. Defaults to `~/.local/share/polypilot`. |
| `POLYMARKET_HOST` | `https://clob.polymarket.com` | Polymarket CLOB API host. |
| `POLYMARKET_PRIVATE_KEY` | _(empty)_ | Polygon wallet private key — required for `live` mode. |
| `POLYMARKET_PROXY_ADDRESS` | _(empty)_ | Polymarket proxy/funder address for live trading. |
| `POLYMARKET_SIGNATURE_TYPE` | `0` | CLOB signature type for live order signing. |
| `POLYPILOT_HTTP_PROXY` | _(empty)_ | HTTP/HTTPS proxy for geo-restricted regions. |
| `FRED_API_KEY` | _(empty)_ | Enables `get_economic_data` (official economy/finance data via FRED). |
| `NEWSAPI_KEY` | _(empty)_ | Optional — broader news coverage in `get_news`. RSS works with no key. |

## Running the MCP server standalone

The MCP server is portable — you can use it from Codex, OpenCode, or any other
MCP client. Run it directly with:

```bash
uv run polypilot-mcp
```

It speaks MCP over stdio. Point your MCP client's server config at this command
(for example, `command: "uv"`, `args: ["run", "--directory", "/path/to/polypilot", "polypilot-mcp"]`).

Optional extras:

```bash
uv sync --extra trade   # live CLOB trading (py-clob-client)
uv sync --extra ml      # Chronos crypto prediction (torch + chronos-forecasting)
```

## Development

```bash
uv sync                 # install core dependencies
uv run pytest           # run the test suite
```

Requires Python 3.11+.
