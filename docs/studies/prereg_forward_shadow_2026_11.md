# Pre-registration: forward scorecard on the live shadow journal

**Status: OPEN. The clock starts at 2026-11-02T12:00:00Z. No proposal at or after that boundary has
been scored, and none has been looked at.** Written before any post-boundary observation, by the DSH
agent on 2026-11-02.

---

## 1. The question

Does the rule the production pipeline actually runs — the EMA trend variant in
`config/forward/06_ema_all_universe_h1.yaml`, on H1 bars, on the six instruments the broker serves
it, with its own stop and target geometry — produce trades whose **net-of-cost R is positive when
measured forward on bars nobody has seen yet**?

This is not a new strategy test. The strategy is already written, already configured and already
running; the only choice made here is to measure it instead of arguing about it.

## 2. Why a forward test is the only honest instrument left

- The pre-registered daily study (`docs/studies/prereg_v2_2026_11.md`) ran on 2026-11-02 and all
  three candidates failed development: every interval straddled zero and every edge went negative
  at double cost (`docs/studies/candidates_2026_11_verdicts.md`,
  `docs/charts/candidates_dev_forest.svg`).
- The hourly version of the same family has been quoted at +84.5% chained out-of-sample in a
  sibling workspace and at +15.5% chained in our own `docs/evidence/gold_ema50200_folds.json`
  (8 folds, 349 trades, 4 of 8 positive). Both were chosen after seeing the data.
- The demo account is being traded by its owner, so a live run of our own would measure his
  decisions, not ours. The shadow pass journals what the pipeline would have opened while holding
  an empty book, and the package has no order surface at all — a send is not declined, it is not
  expressible.

## 3. Frozen inputs

A change to any of these restarts the clock and is recorded in §11.

| File | sha256 (first 16) | Role |
|---|---|---|
| `config/forward/06_ema_all_universe_h1.yaml` | `0920dcd17623e185` | the rule, its geometry and its sizing |
| `scripts/observe_shadow.py` | `646fdf033691d7ac` | the journal that produces proposals |
| `scripts/score_forward_shadow.py` | `e06f8195dcac9df5` | the scorer, frozen here |
| `tests/test_score_forward_shadow.py` | `8c4205cf1b1a09cb` | binds the scorer to this document |
| `docs/evidence/candidates_dev_2026_11.json` | `b7c51bf7e012208c` | source of the cost model and of sd |

The cost constants in the scorer are copies of the registered study's, and
`test_cost_model_matches_the_registered_study` fails if the two diverge for any shared symbol.

## 4. Sample

- Source: `agent_data/shadow_live.db`, `event_type = 'agent_cycle'`, `status = 'OBSERVED'`.
- Unit: one proposal per `(symbol, evaluated_at)`. The watch samples every 20 seconds, so a bar is
  journalled many times; **the earliest sample of a bar wins** and the rest are discarded.
- Boundary: `evaluated_at >= 2026-11-02T12:00:00Z`.
- Bars for scoring: closed H1 from the same terminal, cached to `agent_data/forward_bars/<SYMBOL>.json`
  by `--live` and read from there afterwards, so a re-run is reproducible without the terminal.
- **Burned and excluded:** the 18 proposals at 09:00, 10:00 and 11:00 UTC on 2026-11-02 were seen
  by us before this document was written. They are excluded from the sample; the smoke test in §10
  uses them and is not evidence.

## 5. Fills and exits (identical to the registered daily engine)

- `evaluated_at` is the MT5 bar stamp, i.e. that bar's **open**; the signal bar closes one hour
  later, and the fill is the **open of the next bar**. A signal bar's close is never a fill price.
- Protective levels are live from the entry bar itself. A gap through a level fills at that bar's
  open — worse than the level, and recorded as `stop_gap`/`target_gap`; only when the open is
  inside both levels does the intrabar rule apply, and then the stop is checked before the target.
- Horizon: 24 bars. The time exit is the **open of bar E+24**; if the cached series ends first, the
  exit is the last close and the reason is `end_of_data`; such trades are counted in the report and
  never silently dropped.
- `R` is divided by the proposal's own `risk_distance` — the stop distance the strategy declared at
  the signal bar — so the scorecard measures the strategy against the risk it thought it was taking.

## 6. Costs, and what they already cost per trade

Charged once per round trip: `max(bar spread, floor) × point + 2 × (fixed slippage + bps × price)`,
plus financing at the registered annual rate over held days/252. Double cost multiplies both.

Measured on this broker at 12:30 UTC on 2026-11-02, with the risk distances of that day's live
proposals:

| Symbol | floor (price) | slippage/side (price) | round-trip cost | risk distance | **cost, R** | at double cost |
|---|---|---|---|---|---|---|
| GBPUSD | 0.00002 | 0.00005 | 0.00012 | 0.00273 | 0.044 | 0.088 |
| EURUSD | 0.00002 | 0.00005 | 0.00012 | 0.00245 | 0.049 | 0.098 |
| AUDUSD | 0.00002 | 0.00005 | 0.00012 | 0.00185 | 0.065 | 0.130 |
| NZDUSD | 0.00002 | 0.00005 | 0.00012 | 0.00180 | 0.067 | 0.134 |
| XAGUSD | 0.03000 | 0.02504 | 0.08008 | 0.72029 | 0.111 | 0.222 |
| XAUUSD | 0.20000 | 1.95048 | 4.10097 | 33.15286 | 0.124 | 0.247 |

Why the floors matter: over the last 200 cached H1 bars the broker's own `spread` field is **zero on
74–76% of FX bars and on 71% of silver bars** (XAUUSD median 9 points). The live ask−bid at the same
moment was 0–2 points on the four FX pairs and 50 points on gold. So the field is a best-case quote,
the floor is what gets charged, and for gold the charge is *optimistic*: 20 points billed against a
50-point quote, worth 0.009 R on a 33-point stop. For FX the floor overcharges by about 1 point,
0.004 R. Both are two orders of magnitude below the edge this test would need to find.

## 7. Decision rule

`pass` requires **all four**, and nothing else is a pass:

1. `n >= 200` scored proposals;
2. `days >= 60` distinct entry days (the cluster count);
3. the lower bound of the two-sided **98.33%** moving-block bootstrap interval (block = 3
   consecutive days, wrap-around, 10 000 rounds, seed `20261102`) strictly above zero;
4. the mean at double cost strictly above zero.

98.33% is not a new choice: it is the same Bonferroni-style bar the daily study registered for its
three candidates, kept here for a single candidate because a stricter bar costs nothing and a
looser one would be tuned-by-desire.

Clustering is by **calendar day**, because the pipeline re-proposes the same trend state every hour:
the trades within a day are one bet repeated, not independent bets. Day counts, not trade counts,
set the error bars.

## 8. What this test can and cannot detect — stated before it runs

`detectable = z × sd / sqrt(days)` with `z = 2.394` (the 98.33% two-sided quantile) and `sd` measured
on the scored sample. The daily study measured `sd = 0.997 R` for its gold candidate, so with that
spread:

| days | detectable edge |
|---|---|
| 30 | 0.44 R |
| 60 | 0.31 R |
| 120 | 0.22 R |
| 200 | 0.17 R |

Read this plainly: **a 60-day pass bar can only prove an edge of about a third of an R per trade.**
If the true edge is 0.1 R — which is what a working systematic trader would realistically expect
after costs — this scorecard will not detect it, and the honest outcome is "not detected", not
"no edge". We would need ~200 days to see 0.17 R. The alternative, weakening the interval to 90%,
is not taken: it would buy resolution with the thing we are actually short of, which is a real
result.

## 9. Not allowed after the clock starts

- Changing the policy file, the rule, the geometry, the horizon, the cost constants, the boundary,
  the thresholds or the bootstrap settings.
- Reporting a subset (one symbol, one direction, one month) as the verdict. Per-symbol and
  per-direction breakdowns are printed and are descriptive only.
- Re-scoring the same days after a rule change and calling it a second test. The second test of a
  changed rule has a new clock.
- Moving the goal posts from "net R > 0" to "win rate is high" or "the drawdown is small".

## 10. Recorded readings

| Time | Reading | Status |
|---|---|---|
| ~12:45 UTC 2026-11-02 | Scorer smoke test on the 18 **burned** proposals: 12 scored, 1 day, mean net R **−0.0650**, double cost −0.1449 | implementation check, **not evidence** |
| — | post-boundary scoring | **not run** |

## 11. Amendments

None. Any amendment is appended here with the date, the reason, and the new clock start.

## 12. Early stop

If at day 30 the mean is below −0.20 R **and** the interval lies entirely below zero, the watch is
stopped and recorded as a measured negative. A negative result is a result; spending another 30 days
to confirm it is not science.

## 13. Safety

The owner is trading demo 114929502 himself; this run observes. `mode=observe`, no bridge, no token
read, no order surface in the package, and the documented kill switch is honoured by the watch even
though it could not trade with it off. Nothing here connects to a real account.
