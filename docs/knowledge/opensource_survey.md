# Open-source trading systems: what exists, and how to test one instead of trusting it

Status: survey and evaluation protocol. Copying a bot is not a shortcut — it imports
someone else's overfitting, costs and bugs. This file says what to look for, what the red
flags are, and how to put any candidate through the evaluator that already exists in this
repository (`scripts/evaluate_trades.py`).

## The landscape (by category, not by promise)

**Backtesting and execution frameworks** — infrastructure, worth reusing:
- Backtrader, VectorBT, NautilusTrader, QuantConnect LEAN (Python/C#), zipline-reloaded.
- These do not contain an edge; they contain plumbing (data, fills, metrics). Reusing them
  saves months and changes no probability.

**Crypto bots** — full stacks with live connectors and strategy libraries:
- Freqtrade, Hummingbot, OctoBot, Jesse.
- They ship example strategies (usually trend or grid). The examples are the weakest part
  of each project and are explicitly not profitable claims.

**MT5 expert advisors** — MQL5 codebase and GitHub mirrors:
- Huge volume, mostly martingale/grid ("recovery") systems, many with vendor marketing.
- Repositories that advertise "profitable EA" (for example
  [profitable-expert-advisor](https://github.com/zhutoutoutousan/profitable-expert-advisor))
  ship a backtest report, not an out-of-sample record. A report is a hypothesis.
- Frameworks that export Python strategies to MT5 (for example
  [TradeLab](https://github.com/Czarnak/TradeLab)) are plumbing, and useful as such.

**Signal services and "verified" channels** — unverifiable by construction. A Telegram
channel's screenshots are not a trade log, and the ones with a verified broker feed are
rarely the ones selling subscriptions.

## Why copying fails

1. **Overfitting to the seller's data.** A strategy tuned on 2019–2024 is a description of
   2019–2024. It has no obligation to work in 2026.
2. **Look-ahead and fill assumptions.** Many published backtests fill at the signal bar's
   close, ignore spread, and never gap-fill a stop. Each of those adds a fake edge.
3. **Survivorship.** Repositories that failed are deleted, not archived; the ones you find
   are the survivors of a much larger population.
4. **The cost model.** On gold at 2R targets, a 0.2–0.3R cost per trade moves the
   break-even win rate from 33% to 43%. A bot's backtest without costs is a bot with a
   different strategy than the one you would run.
5. **License and safety.** Many "free" EAs are closed binaries that phone home; an EA runs
   with your terminal's permissions.

## The protocol this repository already supports

For any candidate — a GitHub bot, a paid EA, your own rule — do the same four steps:

1. **Get its trade list**, not its equity curve: date, direction, entry, exit, size.
   If the vendor cannot produce a trade list, there is nothing to evaluate.
2. **Replay it out of sample** on data it was not built on (our `scripts/gold_study.py` and
   `scripts/walk_forward.py` show the pattern: one rule, a development window, a holdout
   opened once).
3. **Charge the real costs** for the venue: spread + slippage + swap/funding.
4. **Score it with `scripts/evaluate_trades.py`**: expectancy in R with a 90% bootstrap
   interval, profit factor, max drawdown, longest losing streak, and the probability of
   ruin at the operator's risk per trade and horizon.

The evaluator answers the only questions that matter — with what probability we win, with
what probability we lose — and it answers them the same way for a famous bot and for a
rule written yesterday.

## Red flags that end the evaluation before it starts

- No out-of-sample period, or "walk-forward" that re-optimises every window and reports
  only the best.
- Win rate above 90%: this is almost always a grid/martingale that hides its tail.
- No drawdown figure, or a drawdown measured on closed trades only.
- A single backtest report as the entire evidence.
- A vendor who will not provide the trade list.
- Any claim of "always profitable", "no loss", "guaranteed".

## What is worth taking from other systems

Not their signals: their **infrastructure and their mistakes**. Backtest engines, data
pipelines, position reconciliation, risk controls and failure post-mortems are reusable.
Their entries are not, because an entry rule without its cost model, its regime and its
out-of-sample record is a story.

## The honest version of "find a profitable niche"

The niche is not found by copying a bot; it is filtered for. Run every candidate through
the protocol above and keep the one whose out-of-sample interval stays above zero after
costs. That filter rejects almost everything, quickly, and it is the only way the answer
"this one is worth risking money on" is ever earned rather than asserted.
