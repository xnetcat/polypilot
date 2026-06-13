# Weather market playbook

Weather has historically been the most reliable category — in predecessor
(PolyDegen) paper/live testing it was the **one category that consistently made
money**, while crypto, sports, and politics consistently lost. The resolution data
is objective (forecast highs from real APIs) and you can cross-reference multiple
sources, so an edge here is real and repeatable. The edge comes from markets that
price a temperature outcome far from where the forecast actually sits. The
recurring winning shape was buying **NO on an exact-temperature market when the
forecast missed the target by several degrees** — the predecessor reported an
~11/11 win streak on exactly that weather-NO exact-temp pattern. Treat that as
strong encouragement to favor this category, but as a prior from a different
system's limited sample, **not** a guarantee: it is a pattern to recognize, not a
rule to apply blindly. Read each question fresh, and confirm the edge holds in
YOUR own resolved record via `get_performance_stats()` / `get_scorecard()` —
adjust if your data disagrees.

## Market types — parse the question carefully (direction errors are costly)
- **Exact temp** ("highest temperature 22°C"): YES wins only if the high lands in
  that exact bucket. These are LOW-probability YES bets — favor NO unless the
  forecast centers tightly on the bucket with low spread. The detector that flags
  these looks for `"highest temperature"` plus a single bare degree value with no
  range and no `between`.
- **Threshold** ("25°C or higher" / "or lower"): directional. "or higher"/"above"
  → YES wins if forecast_high ≥ target; "or lower"/"below" → YES wins if
  forecast_high ≤ target. Mis-reading direction has lost real money (a prior
  session lost ~$12 on a Beijing market by flipping the direction). Look for the
  words `above` / `or higher` / `or above` (→ above) versus `below` / `or lower` /
  `or below` (→ below), and re-read once before sizing.
- **Range** ("between 18 and 21°C"): YES wins if the high lands inside the range.
  Watch the boundaries specifically — a forecast sitting AT the edge of the range
  (e.g. 71°F for a 70-71°F market) makes the range outcome MORE likely, not less,
  so NO is weaker than it first looks.

## Using `get_weather_forecast(question)`
- Compare `forecast_high` to the target WITH a margin for forecast uncertainty.
  If the forecast is within ~1°C of the boundary, treat it as a coin-flip and
  likely skip — the forecast error band swamps the edge. Tropical/high-variance
  locations need a wider margin than temperate ones.
- Tighter forecast spread + larger margin to the boundary = higher confidence.
  When two independent forecasts agree and both sit several degrees off target,
  that is the high-conviction setup.
- A wide forecast spread (e.g. ~2°C between sources) is itself a signal of model
  uncertainty — the true high could land on the target. Be cautious near the
  boundary in that case.
- Beware resolution-day information asymmetry: if the market resolves today, the
  actual high may already be partly determined while the forecast still shows a
  range. The market price may know something your forecast snapshot does not.
- A strong historical NO win-rate on similar markets is encouraging but is a small
  sample — don't let it override a thin forecast margin on the specific market in
  front of you.

These are judgment aids, not gates. There is no fixed confidence floor or margin
threshold — weigh the forecast margin, source agreement, spread, and boundary
position together, and skip when the edge is thin.
