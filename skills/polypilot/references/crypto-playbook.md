# Crypto market playbook

Use `get_crypto(question)` for spot, 24h change, history, and (if the `ml` extra
is installed) a Chronos direction prediction.

Crypto has been a harder category than weather — much harder. In predecessor
(PolyDegen) paper/live testing, crypto (alongside sports and politics)
**consistently lost money**, while weather was the only category that consistently
profited. Threshold / "up or down" / short-dated candle markets are close to a
coin flip after fees, so even with correctly verified spot data a confident-looking
bet can lose because the move you are betting against is routine volatility. In a
past session a YES bet that BTC stayed ~1.7% above a strike over 17 hours lost — a
1.7% daily move is normal for Bitcoin. The lessons: **raise your edge bar, size
crypto SMALLER than weather, demand a real volatility-aware margin (not just a
correct current price), and default to skipping when the read is marginal.** Treat
this as an inherited prior from a limited sample, not a law — confirm it against
YOUR own resolved record via `get_performance_stats()` / `get_scorecard()` and
adjust if your data disagrees.

## Market shapes
- **Threshold markets** ("BTC above $70k by Friday"): compare spot to the strike
  AND the time remaining. Far-from-strike with little time → high confidence on
  the near side; near-strike, especially with lots of time left, → low confidence,
  usually skip. Translate the gap into "how many normal daily moves away is the
  strike?" — a strike within one typical daily move is effectively a coin-flip.
- **Short-dated candle markets** (5-min / hourly up-or-down): treat these as
  **near-random** — they were a reliable money-loser in predecessor testing. The
  default here is to **skip**; only trade with a genuinely strong, well-grounded
  signal (clear momentum or a confident model prediction), and even then size
  **tiny**. Pure odd/even or coin-flip-style framings are traps — skip them.
  - **Finding them:** they're low-volume, so the default volume-sorted
    `list_markets` won't show them — use `list_markets(category="crypto",
    closing_within_hours=1)` (an end-date-window query) to surface the live
    candles. These markets also carry a **higher fee** (rate ~0.07 vs ~0.03–0.05
    elsewhere), which near 0.5 is ~1.7% — pass the market's real `fee_rate` to
    `compute_edge`. Use a **market order** (the tiny size won't meet the 5-share
    limit floor) and remember the kill-switch is your whole bankroll.

## Using the inputs together
- Don't over-trust the Chronos prediction; treat it as one input alongside spot,
  momentum (24h change / price history), and the order book.
- A correct spot price is necessary but not sufficient — the real question is the
  probability the price STAYS on your side through resolution given the asset's
  volatility and the time remaining.
- When the order book leans heavily against your side, re-examine the thesis
  before sizing (see `reading-order-books.md`).

There are no hardcoded thresholds here — these are framing aids. Demand a clear,
volatility-aware margin and be quick to skip.
