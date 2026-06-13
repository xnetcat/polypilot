# Reading order books

`get_order_book(token_id)` returns best bid/ask, spread, midpoint, and
imbalance = (bid_volume - ask_volume) / total.

- **Imbalance > 0**: bid-heavy → buyers leaning in (supports YES-token strength).
- **Imbalance < 0**: ask-heavy → sellers leaning in.
- **Wide spread**: thin/illiquid — your paper fill at best ask may not reflect a
  realistic live fill. Be more conservative on size.
- **Use the midpoint, not last price**, as your reference "fair-ish" market price
  when computing edge.
- A divergence between price move and imbalance can signal a near-term reversal,
  but on resolution-driven markets the underlying data (forecast, score) should
  dominate your thesis — not microstructure.

## When the book contradicts your thesis
A strongly ask-heavy book (e.g. imbalance around -0.8) on a token you want to buy
NO of means smart money is buying YES against you. That is a reason to re-examine
your thesis, not to ignore the book — in past sessions an ask-heavy order book on
a weather NO was a genuine warning sign. Treat a sharp imbalance against your side
as a prompt to double-check the underlying data before sizing up.
