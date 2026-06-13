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
  until you stop it or the kill-switch trips. A one-shot variant (`/polypilot-cycle`)
  runs a single cycle and stops, for scheduled/unattended trading.
- **MCP server (`polypilot-mcp`)** — a [FastMCP](https://github.com/modelcontextprotocol/python-sdk)
  Python server exposing the tools below, backed by **SQLite** state (positions,
  trades, market intelligence, portfolio) with separate database files for paper
  and live. The kill-switch lives here.

## Multi-agent mode

On clients that support subagent dispatch (Claude Code's `Task` tool), Polypilot
parallelizes its research and adds an adversarial check, using two subagents
bundled in the plugin's `agents/` directory and auto-discovered on install. Both
are **research-only** — neither can place or close orders; execution stays with
the orchestrator and the kill-switch.

- **`polypilot-researcher`** — handed one market, it gathers data with the
  research, market, and order-book tools and returns a structured verdict
  (fair probability, recommended side, confidence, edge, thesis, key evidence).
  Each cycle the orchestrator fans these out **in parallel** over the top few
  candidates (capped per cycle to bound cost) and ranks them by the returned
  edge.
- **`polypilot-skeptic`** — before a sizeable trade, it red-teams the proposed
  thesis: it hunts for disconfirming data, checks the order book for adverse
  flow, and re-tests the edge, then reports whether the trade survives (with a
  more conservative fair-probability estimate). The trade goes through only if it
  survives; small trades can skip this step to save cost.

This is an **optional accelerator**. On clients without subagents (e.g. Codex),
the agent does the same research and red-teaming inline with the same MCP tools —
the loop, the rules, and the decisions are identical.

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

## Unattended / scheduled mode

For hands-off trading on a schedule, use the bundled **`/polypilot-cycle`** skill
instead of the continuous `/polypilot` loop. It runs **exactly one** cycle —
safety check → reconcile/review → discover → research → decide → trade → record
evaluation (with stake) → report — and then **stops**, leaving the cadence to a
scheduler. The doctrine, rules, kill-switch, and tools are identical to
`/polypilot`; only the control flow differs (one pass, no `wait_for_next_cycle`,
no loop).

Drive it from Claude Code's `/schedule` or from cron — for example, a routine
that runs every 30 minutes:

```
/schedule create "polypilot trading" --cron "*/30 * * * *" --prompt "/polypilot-cycle"
```

or via system cron invoking Claude Code headless:

```cron
*/30 * * * * cd /path/to/your/session && claude -p "/polypilot-cycle"
```

A kill-switch trip still halts the run: `/polypilot-cycle` reports it and exits
without trading. Pair scheduled runs with **Notifications** (below) so fills and
breaker trips reach you while you're away.

## Notifications

Polypilot can push alerts (fills, kill-switch trips) to an external channel,
which is especially useful in unattended/scheduled mode. Configure either or both:

| Variable | Description |
|----------|-------------|
| `POLYPILOT_WEBHOOK_URL` | Generic webhook URL; messages are POSTed to it. |
| `POLYPILOT_TELEGRAM_BOT_TOKEN` | Telegram bot token (used with the chat id below). |
| `POLYPILOT_TELEGRAM_CHAT_ID` | Telegram chat id to deliver messages to. |

Behavior: the agent can call `send_notification` directly, and the server
**auto-notifies** on order fills and on a kill-switch trip when a channel is
configured. If nothing is configured, notifications are a no-op and never block
trading.

## Multi-outcome markets

Markets are not always simple YES/NO. `list_markets` and `get_market` may return
markets with `is_binary: false` and an `outcomes` list (e.g. a multi-candidate
race). To trade one, the agent picks a specific outcome's `token_id`, estimates
that outcome's fair probability, and treats it as a "YES on that token" — an
outcome token pays $1 if its outcome wins — using
`compute_edge(fair_prob, "YES", <token price>)` for edge math and passing the
outcome label as the `side` to `place_order`.

## Paper vs live mode

| Mode | How to enable | Behavior |
|------|---------------|----------|
| **paper** (default) | nothing (or `POLYPILOT_MODE=paper`) | Simulated fills against the live order book. No real money. |
| **live** (opt-in) | `POLYPILOT_MODE=live` + `POLYMARKET_PRIVATE_KEY` (install the `trade` extra) | Real orders via the Polymarket CLOB. |

Paper and live use **separate SQLite database files** (`paper.db` / `live.db`),
so paper experiments never touch live state.

### Live accounting & restart reconciliation

The two modes treat their local ledger differently:

- **Paper** — the SQLite ledger *is* the truth. It is fully self-consistent and
  authoritative; there is nothing on-chain to reconcile against, so paper mode
  needs no reconciliation at all.
- **Live** — the local ledger is only a *model* of on-chain reality (your real
  USDC balance and ERC-1155 outcome positions). It can drift from the chain after
  a restart or any downtime — fills that landed while the process was down, manual
  transfers, externally-opened positions. So in live mode the local books are
  **anchored to the chain** by a startup reconciliation:
  `preflight_live` (verify key, creds, balance, kill-switch — STOP if not `ok`) →
  `sync_live_balance` (true-up local cash to the real on-chain USDC) →
  `import_live_positions` (adopt on-chain positions not tracked locally) →
  `sync_live_positions` (report any remaining drift). **Run this after any
  restart**, before trading.

This reconciliation runs **automatically at the start of a live `/polypilot` or
`/polypilot-cycle` session** — the skills perform it as a live-only preamble
before any order is placed (and skip it entirely in paper mode). The kill-switch
still halts trading at any time, and no real order is placed until `preflight_live`
passes.

In live mode, **`get_live_account` is the authoritative account view** — it pulls
the real USDC balance and open positions directly from Polymarket, marks each
position at its live order-book midpoint, and returns balance, positions, equity,
total position value, and unrealized PnL from chain truth rather than the local
model. Trust it over the modeled local cash/positions when the two diverge.
**Realized PnL, however, is still derived locally** (from the SQLite trade
ledger): Polymarket exposes no direct realized-PnL endpoint, so realized PnL —
and the kill-switch that keys off it — remains a locally-computed figure.

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

## Tool surface (36 tools)

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
- `place_order` — buy a side for a USD amount, storing your thesis/confidence/fair_prob; **enforces the kill-switch** and refuses when tripped or cash is short. Supports `order_type` `market` (default; slippage-walked in paper, FAK in live) or `limit` (GTC resting order at `limit_price`).
- `reconcile_positions` — settle open positions whose markets resolved, crediting payouts and updating realized PnL.
- `close_position` — sell an open position now at best bid (to recycle capital from near-resolved positions, not as a stop-loss).

**Live trading ops** (no order is placed; the reconciliation tools true-up local state)
- `preflight_live` — pre-flight checklist before going live: key set, derivable CLOB creds, positive balance, kill-switch configured. Returns `{ok, checks}`; never raises. Run first at a live session start; if not `ok`, do not trade.
- `sync_live_balance` — true-up local cash to the real on-chain USDC, correcting drift or restart staleness. Run at live startup before trading.
- `import_live_positions` — adopt on-chain positions not yet tracked locally into local state (no cash debit, no buy logged), so live tracking survives a restart; reconciles the drift `sync_live_positions` only reports.
- `sync_live_positions` — diff on-chain positions vs. the local ledger (`on_chain_only` / `local_only` / `matched` / `in_sync`) without mutating state.
- `get_live_account` — **authoritative** Polymarket-sourced account snapshot: real USDC balance, real positions marked at live midpoints, and the resulting equity, total position value, and unrealized PnL (`{source, balance_usd, equity, total_position_value, total_cost, unrealized_pnl, positions, marked, unmarked}`). Trust this over the modeled local cash/positions in live mode. Read-only; never raises.
- `get_live_balance` — on-chain USDC (collateral) balance from the CLOB. Read-only.
- `get_open_orders` — list your resting (unfilled/partially-filled) limit orders on the CLOB; `{orders: [...]}` or `{error}` with no key. Never raises.
- `cancel_order` — cancel a single resting order by id (ids from `get_open_orders`); `{status: cancelled, order_id}` or `{status: error, reason}`. Never raises.

**Backtesting / calibration**
- `get_scorecard` — decision-quality scorecard over your resolved trades (win rate, ROI, Brier score, log loss, and a 10-bucket calibration table), overall and per category; consult it to calibrate your own confidence.
- `list_resolved_markets` — recently resolved real markets with their winning outcome, to backtest/practice forecasting against.

**Portfolio / Memory**
- `get_portfolio` — cash, realized PnL, open count, exposure, mode; best-effort marks open positions to market (book midpoints) to add `unrealized_pnl`, `mark_to_market_value`, and `total_pnl`.
- `get_positions` — open (or closed) positions with entry, size, shares, and stored thesis.
- `get_trade_history` — recent buy/sell trades with prices and outcomes.
- `get_performance_stats` — win rate, total/avg PnL, and ROI from resolved evaluations, broken down by category (optional `category` filter).
- `get_session_report` — one-shot snapshot: portfolio, risk status, performance stats, recent trades, and open-position count.
- `record_thesis` — attach/refresh a written thesis, confidence, and fair probability on an open position.
- `get_similar_markets` — your historical track record on past markets matching keywords (powers candidate screening).

**Safety / Pacing**
- `get_risk_status` — cash, PnL vs. kill-switch limits, breaker state, mode. (Call first each cycle.)
- `wait_for_next_cycle` — server-side sleep that paces the loop and may return early if a held position nears resolution.
- `ping` — health check.

**Notifications**
- `send_notification` — push a message to the configured webhook and/or Telegram channel (e.g. a fill or a kill-switch alert). No-op when no channel is configured; never blocks trading.

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
| `POLYPILOT_WEBHOOK_URL` | _(empty)_ | Generic webhook for notifications (fills, kill-switch). |
| `POLYPILOT_TELEGRAM_BOT_TOKEN` | _(empty)_ | Telegram bot token for notifications (with chat id). |
| `POLYPILOT_TELEGRAM_CHAT_ID` | _(empty)_ | Telegram chat id for notification delivery. |
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
