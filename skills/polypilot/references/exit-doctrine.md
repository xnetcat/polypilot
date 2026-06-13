# Exit doctrine

Binary markets resolve to $0 or $1. Intermediate price swings are noise you only
realize as loss if you sell into them. Liquidity on niche markets is thin —
round-tripping crosses a wide spread twice and eats the edge.

**Default: hold to resolution.**
- Enter on a thesis. Re-check the thesis each cycle against fresh tool data.
- Exit early ONLY when new hard data conclusively invalidates the entry thesis
  (e.g. the forecast flipped past your target with margin). A price drop alone is
  NOT invalidation.
- **No mechanical stop-losses.** Ever.

**Capital recycling (the one active move):**
- If a position has run to ~0.95+ (near-resolved), its remaining upside is tiny
  and its capital is locked. You MAY `close_position()` to redeploy into a
  clearly better edge you've found this cycle. This is opportunity-cost
  reasoning, not a stop-loss — only do it when you have a concrete better use for
  the capital.
