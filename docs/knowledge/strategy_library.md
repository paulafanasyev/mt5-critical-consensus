# Strategy library: what the open literature offers, and what it survived here

A catalogue of publicly documented approaches, each with its claim, the quality of the
evidence behind it, whether this repository has tested it, and what happened. It is a
working map, not a menu of profitable systems: every entry that was tested here failed
after costs.

## How to read the evidence column

- **A** — replicated in peer-reviewed work on multiple markets and periods.
- **B** — documented in books or practitioner literature, with some out-of-sample work.
- **C** — vendor/blog claim, no independent replication, often a backtest screenshot.

The evidence grade is about the *documented claim*, not about whether it will work now.

## The families

| # | Family | Claim | Evidence | Tested here | Our result |
|---|---|---|---|---|---|
| 1 | Time-series momentum (Moskowitz, Ooi, Pedersen 2012) | an asset's own past return predicts its next-month return | A | partly | trend rules on gold, FX, crypto: no positive net R |
| 2 | Cross-sectional momentum (Jegadeesh & Titman 1993) | past 3-12 month winners beat losers | A | no | needs a cross-section of many assets; we hold too few |
| 3 | Trend following / Donchian (Turtle rules) | breakouts of N-day extremes trend | B | yes | gold, silver, FX: negative net R |
| 4 | Moving-average crossover (EMA/SMA) | fast above slow = uptrend | B | yes | negative on every venue tested |
| 5 | Mean reversion, Bollinger/RSI | price returns to a moving average | B | yes | negative or noise; gold 4/8 folds |
| 6 | Opening-range breakout (Zarattini & Aziz 2023, equities) | first-hour range break continues | B | yes (gold H1) | 1 193 trades, 21.9% wins, −0.96R |
| 7 | Pairs / relative value (gatev 2006) | a spread between two linked assets reverts | A (equities) | yes (gold/silver) | 179 trades, −0.66R |
| 8 | Calendar effects (turn-of-month, day-of-week) | seasonality in returns | B/C | yes (gold daily) | intervals all straddle zero after costs |
| 9 | Volatility risk premium (selling options) | implied vol exceeds realised | A | no | needs options data we do not have |
| 10 | Carry (Koijen et al. 2018) | high-yield currencies/commodities outperform | A | no | needs futures curve and rates |
| 11 | Event-driven / news | a scheduled event moves price predictably | B/C | no | needs a timestamped event database |
| 12 | Market making / liquidity provision | earn the spread | A | no | needs exchange connectivity and inventory risk |
| 13 | Statistical arbitrage across many names | residual mean reversion | B | no | needs a wide universe and shorting |

## What the failures have in common

1. **Costs relative to the risk unit.** A 2R target with a 0.2–0.3R round-trip cost needs a
   43% win rate to break even; documented rules deliver 30–43%. ORB showed the extreme
   case: when the stop is smaller than the spread, every trade loses roughly one R.
2. **Selection.** Every "profitable" result we reproduced was the best cell of a grid or
   the best season of several. The neighbours were negative.
3. **Regime.** The gold trend that looked strong in 2023–2026 was absent (negative) in
   2020–2023 on the same rules.

## The books worth reading, and why

- Moskowitz, Ooi & Pedersen, *Time Series Momentum* (2012) — why trend can persist, and the
  volatility-scaling it requires.
- Jegadeesh & Titman (1993) — momentum's original evidence and its reversal horizons.
- Pardo, *Design, Testing, and Optimization of Trading Systems* — the walk-forward method
  this repository uses.
- Chan, *Algorithmic Trading* — practical sizing and mean-reversion construction.
- López de Prado, *Advances in Financial Machine Learning* — why most backtests are
  overfit, and how to split data without leakage.
- Gatev, Goetzmann & Rouwenhorst (2006) — the pairs-trading benchmark.

None of these books contains a rule that survives retail costs by itself; they contain the
method for testing whether one does.

## What to test next, in order

1. Families 2, 9, 10 (cross-sectional momentum, volatility premium, carry) — they need data
   this repository does not have, and they are the ones with the strongest academic record.
2. Family 11 (event-driven) — only with a timestamped event database and a pre-registered
   window; otherwise it is storytelling.
3. Nothing from grade C without an out-of-sample record that the seller will publish.

## Bottom line

Thirteen documented families; eight testable with the data here; all eight failed after
costs. The map says where to look next, not that anything there is profitable.
