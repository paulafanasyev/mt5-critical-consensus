# Gold: what moved it, 2020–2026 — facts, reported drivers, and what cannot be concluded

Status: **revised after review; contested where marked.** This is not a trading system and
not evidence of an edge. It records what happened, what was reported at the time, and — after
a hostile review — what is wrong with the first version of this file.

## Why this file exists

The programme behind it has failed three times to find an edge, most recently with six
pre-registered rules on 54 119 hourly gold bars, all negative after costs. The temptation
now is to explain the past with stories and call that knowledge. This file tries to avoid
that by grading the evidence instead of asserting causes.

## The instrument problem (the review's first finding, confirmed here)

The price series used initially was **PAXG/USDT, a token**, not spot gold. Normally it
tracks spot closely: over 880 overlapping days the premium was median **−0.06%**, 10th
percentile **−0.97%**, 90th **+0.60%**. But it decoupled exactly during the episode the
first version narrated:

| Date | PAXG close | XAUUSD close | Premium |
|---|---|---|---|
| 2025-10-16 | 4 436.8 | 4 325.9 | **+2.56%** |
| 2025-10-17 | 4 238.9 | 4 252.6 | −0.32% |
| 2025-10-21 | 4 098.3 | 4 124.9 | −0.64% |
| 2026-01-30 | 4 924.4 | 4 894.5 | +0.61% |
| 2026-02-05 | 4 700.0 | 4 777.9 | −1.63% |

So the "record high +4.7%" on 2025-10-16 is part gold, part premium expansion, and the
2024-04-13 row is a **Saturday token print** — spot did not trade that day, and the first
liquid session reversed. **Do not use PAXG levels from these dates in any backtest.**

## The largest moves, graded

Grade A = a contemporaneous source supports this date and this number; B = covers the
event but not the number; C = no contemporaneous source, or the "driver" only restates
the price path.

| Date | Token move | Reported driver (graded) | Grade |
|---|---|---|---|
| 2026-01-30 | −10.1% | Fed shock; metals sold off together | B |
| 2026-01-28 | +6.7% | *(none: "reversal" is only visible after the 30th)* | C |
| 2026-02-05 | −6.6% | *(none: "continuation of turmoil" restates the path)* | C |
| 2025-10-21 | −6.1% | biggest one-day fall in 12 years after a record run | A ([Yonhap](https://www.yna.co.kr/amp/view/AKR20251022000652072)) |
| 2025-10-16 | +4.7% | record high on **trade tensions** — the cited source says trade, not geopolitics | A, but the label was wrong |
| 2026-08-05 | +5.0% | Hormuz reopening hopes + Fed expectations + technical break | B (reported; not linked) |
| 2024-04-13 | +4.1% | Saturday token print; spot did not trade; Monday reversed | C |
| 2020-11-09 | −4.5% | vaccine efficacy announcement, risk-on | C (not linked) |
| 2021-01-08 | −4.3% | yields jumped after the Georgia run-off | C (not linked) |

Two of the nine rows have no driver at all, and three more have no link. The previous
version called only one of them unverified, which was worse than calling none.

## What actually recurs — stated as a chain, not four labels

The first version split one causal chain into four "classes" that overlap by construction
(Fed → real yields → positioning → liquidation). Counting the January 2026 day once, the
recurring structure is:

**trigger → channel → realisation**

- Trigger: a scheduled or surprise macro event (Fed communication, CPI, NFP, a
  geopolitical headline).
- Channel: the dollar and real yields, plus positioning built up before the event.
- Realisation: the move itself, whose *sign* depends on how crowded the positioning was,
  which is why the same class of trigger produces both signs.

The sign is not predictable from the class. Two counterexamples inside the sample, both
from contemporaneous press:

- 2026-08-05: Hormuz de-escalation → gold **+5.0%**.
- 2026-10-31: escalating Hormuz tensions → gold **fell** to 3 874.4.

"Escalation lifts gold" is therefore refuted twice in three months by the same kind of
event. The correct statement is: escalation can lift or sink gold depending on what it
implies for policy and for crowded positioning.

## What the data can and cannot support

**Can:** the sample has daily sd **0.98%**; 31 of 2 256 days moved ≥3%; the biggest single
days cluster around reversals of long trends. That clustering is the standard negative-skew
/ leverage effect of most financial series and is guaranteed by a sample that ran
+178% (1 968 → 5 480) then −29% (5 480 → 3 874); it is not evidence of a tradable "record
reversal" rule.

**Cannot:** any claim that a driver class predicts direction; any claim that a story about
a past move is knowledge about the next one; any claim that these moves are tradable after
costs — with 2R stops several times the daily sd, the cost-to-R ratio dominates.

## What a testable version needs (and the first version omitted)

1. **An event database** with publication timestamps to the minute, a named source, a
   version, and an objective inclusion rule per class.
2. **A declared window and baseline**, with volatility scaling and a market model, not a
   naive "same hour on non-event days".
3. **A declared test family and FDR control** — Benjamini–Hochberg at q = 0.10 over every
   event class × window × direction actually examined. Without this the design reproduces
   the three prior failures by construction.
4. **A cost model with numbers** and an acknowledgement that a ±30-minute window cannot be
   executed on daily token data; the resolutions do not match.
5. **A power analysis**: ~50 FOMC and ~70 CPI events in six years, split by sign and
   regime, leaves 15–25 per cell against a 0.98% daily sd — most cells cannot decide.
6. **A stated null and a falsification criterion**, written before the run.

## Bottom line

This file is documented memory with grades attached. It is not a source of signals, and it
must not be promoted out of `docs/knowledge/` as one. The only thing it reliably
establishes is negative: the popular one-line explanations of gold's big days do not
survive contact with their own sources.
