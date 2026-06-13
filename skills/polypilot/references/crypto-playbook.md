# Crypto market playbook

Use `get_crypto(question)` for spot, 24h change, history, and (if the `ml` extra
is installed) a Chronos direction prediction.

Crypto has been a harder category than weather: even with correctly verified spot
data, a confident-looking bet can lose because the move you are betting against is
routine volatility. In a past session a YES bet that BTC stayed ~1.7% above a
strike over 17 hours lost — a 1.7% daily move is normal for Bitcoin. The lesson is
to size crypto more cautiously and demand a real margin, not just a correct
current price.

## Market shapes
- **Threshold markets** ("BTC above $70k by Friday"): compare spot to the strike
  AND the time remaining. Far-from-strike with little time → high confidence on
  the near side; near-strike, especially with lots of time left, → low confidence,
  usually skip. Translate the gap into "how many normal daily moves away is the
  strike?" — a strike within one typical daily move is effectively a coin-flip.
- **Short-dated candle markets** (5-min / hourly up-or-down): these are close to
  random. Only trade with a real signal (strong momentum or a confident model
  prediction), and size small. Pure odd/even or coin-flip-style framings are
  traps — skip them.

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
