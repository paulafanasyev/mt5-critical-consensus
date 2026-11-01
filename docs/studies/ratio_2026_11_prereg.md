# Pre-registration — gold/silver ratio reversion, stamped 2026-11-01 17:5x

Status: written before the rule was run, after the failure of every directional family in
this repository. One hypothesis, one development window, one holdout, and the right to say
no.

## Why this idea and not another

Every family tested so far (trend, breakout, mean reversion, momentum, session, volatility
filter, calendar effects) was directional: it bets on gold alone. The gold/silver ratio is
a different object — a **relative** price between two metals that share most of their
drivers. If something in this programme can survive costs, it is more likely to be a
relative-value structure than another directional rule, because the two legs share the
common factor and the trade does not need to forecast the gold price at all.

## Data

- XAUUSD H1 and XAGUSD H1 from the MetaQuotes terminal, aligned on the same closed hour.
- Expected sample: 2023-06 → 2026-10, roughly 20 000 hourly bars per leg.
- **Development: 2023-06-06 .. 2025-06-30.** **Holdout: 2025-07-01 .. 2026-10-30**, opened
  once, only if development passes. This holdout has not been used for a ratio rule before.
- Bars are the terminal's own; the server offset is 7200 s and both legs are quoted in USD.

## The rule, exactly

- ratio = XAUUSD close / XAGUSD close on the same closed hour.
- z = (ratio − SMA(100)) / STD(100), population standard deviation, window of the last 100
  closed hours including the current one.
- **Entry:** long the ratio (buy gold, sell silver) when z ≤ −2.0; short the ratio (sell
  gold, buy silver) when z ≥ +2.0. Entry at the **next hour's open**, both legs at once.
- **Exit:** at the open of the first hour whose z crosses 0; or at a stop when |z| ≥ 3.5;
  or after 120 hours, whichever comes first.
- One position at a time. No re-entry in the same hour as an exit.
- Sizing: equal **risk** on both legs, each leg 0.25% of a 10 000 account, stop distance =
  3.5σ of the ratio converted to each leg's price; the two legs are sized so that a stop
  loses the same amount on both.
- Costs: 0.15% round trip per leg (spread + slippage), i.e. 0.30% on the pair per round
  trip, charged once on entry and once on exit.

## Metric and decision rule

- Primary: mean net R per trade with a **stationary block bootstrap** (block length 24
  observations, 10 000 resamples, seed 20261101), 95% one-sided lower bound.
- One hypothesis, so no multiplicity correction; the threshold is the ordinary 5%.
- Development passes if the lower bound of the mean net R is above zero with n ≥ 40 trades.
- The holdout is then opened once and must repeat the result with n ≥ 30.
- A pass is a candidate for the demo forward test, nothing more.

## What "no" looks like

If development's interval includes or lies below zero, the idea is rejected and recorded,
and no parameter is re-tuned. If development passes and the holdout fails, the idea is
rejected as a development artefact. Both outcomes are final for this rule.

## Known risks that would invalidate the test

- **Asynchronous fills.** The two legs are executed at the same hour's open in the model;
  in reality they fill seconds apart and the ratio moves. Any real edge must exceed that
  slippage, which the 0.30% cost is meant to cover.
- **Silver liquidity.** XAGUSD spreads widen at rollover; the cost assumption may be
  optimistic exactly when the stop is nearest.
- **A regime where the ratio trends.** Ratio reversion assumes the ratio is stationary; if
  it trends (as it did in 2020-2021), the rule buys every dip and loses. The 3.5σ stop
  bounds one trade, not the regime.
- **The holdout overlaps a period already examined for gold direction.** It has not been
  used for a ratio rule, but it is not virgin data; this is stated rather than hidden.

## Stamp

Author: DSH agent. Reviewer: the same hostile review process used for the failed gold
study; the review is attached before the development run is interpreted.
