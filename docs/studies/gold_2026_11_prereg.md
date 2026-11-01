# Pre-registration — gold study, stamped 2026-11-01

> **Status: REJECTED by review on 2026-11-01, after the first development run.**
> The claim below that this was "written before any hypothesis" is false. The holdout
> period had already been examined: `docs/evidence/gold_ema50200_folds.json` (13:29,
> 27 minutes before this stamp) covers 2025–2026, and `standalone/reports/gold_research_report.txt`
> (2026-10-31 20:18) already ran PAXG 2020–2026 folds whose out-of-sample included 2024–2026.
> A holdout that has been seen is not a holdout, so this document cannot support any test
> on that period. The review also found the six rule clauses ambiguous enough to change
> trade counts by an order of magnitude, and the design powered only for edges of ~0.25R
> or more, which the cost model makes implausible. It is kept as a record of a rejected
> design, not as a protocol to run.

Written before any hypothesis in this study was run. The point of the stamp is that the
rules, the split, the costs and the decision rule cannot move after the results are seen.
If the data says no, the study reports no.

## Data

| Venue | Series | Bars | Period |
|---|---|---|---|
| Binance PAXG/USDT | daily | 2 257 | 2020-08-28 .. 2026-11-01 |
| Binance PAXG/USDT | hourly | 54 119 | 2020-08-28 .. 2026-11-01 |
| MetaQuotes XAUUSD | hourly | 20 000 | 2023-06-06 .. 2026-10-30 |

PAXG is tokenised gold quoted in USDT; it is an independent venue from the broker, which
is why it is the primary sample. XAUUSD is a confirmation sample only: its history is
shorter and overlaps the holdout, so it can never rescue a rule that fails on PAXG.

## Split

- **Development:** 2020-08-28 .. 2023-12-31. Everything may be looked at here.
- **Holdout:** 2024-01-01 .. 2026-11-01. Opened exactly once, for the rules frozen below,
  after the development result is recorded. No parameter may change afterwards.

## Rules under test (exactly six, no additions)

All rules run on closed bars only, one position at a time, long and short.

1. **Trend (EMA).** EMA(50) vs EMA(200) on the series' native timeframe. Long when
   EMA50 > EMA200 and close > EMA200; short when EMA50 < EMA200 and close < EMA200.
   Exit: opposite condition, stop, or target.
2. **Breakout (Donchian).** Long when close > highest high of the previous 20 bars,
   short when close < lowest low of the previous 20 bars. Exit: stop/target or a close
   against the previous 55-bar extreme.
3. **Mean reversion (Bollinger).** Enter against a close outside the 20-bar 2-sigma band;
   exit at the 20-bar mean, stop, or target.
4. **Momentum (ROC).** Enter in the direction of the 24-bar rate of change when
   |ROC| >= 2%; exit after 24 bars, at the stop, or at the target.
5. **Session breakout.** On the hourly series, take the high/low of the 00:00–08:00 UTC
   window and enter on a close beyond it during 08:00–20:00 UTC, once per day.
6. **Volatility filter.** Rule 1 plus: trade only when ATR(14) is between the 25th and
   90th percentile of the trailing 500 bars.

Stops and targets are identical for every rule: stop at 2 × ATR(14) from the entry,
target at 2R. Entry at the next bar's open, never at the signal bar's close.

## Costs

- PAXG: 0.1% per side (Binance taker), charged as a 0.2% round-trip spread.
- XAUUSD: the broker's spread from the bar plus 0.05% slippage per side.

## Sizing

Fixed 0.5% of a 10 000 account per trade, notional capped at 3× equity, one position.
No compounding, so R is comparable across trades.

## Metric and decision rule

- Primary metric: **mean net R per trade** with a 90% bootstrap confidence interval
  (10 000 resamples, seeded).
- Multiplicity: 6 rules × 2 venues = 12 tests, so the development threshold is
  **one-sided p < 0.0042** (Bonferroni at family-wise 5%), which for the bootstrap means
  the interval's lower bound at the 99.58% level must exceed zero.
- A rule advances only if it passes on **PAXG development**. XAUUSD is not a tie-breaker.
- The holdout is then opened once. A rule passes the study only if the holdout's 90%
  interval also lies above zero with at least 100 trades.

## What counts as "no"

If no rule passes development, the study reports "no edge found in these six families on
gold 2020–2023" and stops. If a rule passes development but fails the holdout, the study
reports "the development result did not survive out of sample". Both are results.

## Explicitly out of scope

- Any search over parameters (the numbers above are fixed; no grid).
- Any rule added after seeing development results.
- Treating the demo forward run as evidence of edge: it is an execution-integrity test.
- Any claim of "always profitable"; the study can only support "positive mean R with a
  confidence interval above zero on this sample", and even that is not a guarantee.

## Stamp

Author: DSH agent. Reviewed by: two independent critics (to be attached). Data hashes are
recorded with the first run. Prior studies in this repository found no edge on FX, indices
or crypto, and a borderline result on gold that did not survive proper measurement.
