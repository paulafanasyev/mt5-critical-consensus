# Verdicts — the three pre-registered 2026-11 daily candidates, development window

**Run:** 2026-11-02, once, with the protocol frozen in `prereg_v2_2026_11.md`. Nothing was tuned
before, during or after this run; the engine is the committed `scripts/candidate_study.py`
(`9f5a2ad` + `2a6f8b6`) and the data is the frozen cache whose manifest sha256 starts
`73d4359f901e`.

The command was then re-invoked with `--cost-sweep` — §11's own reproducibility line — so the
console transcript could be archived beside the JSON. Both invocations printed byte-identical
numbers. That is a reproduction, not a second attempt: no rule, cost, window or parameter moved
between them, and the sweep only re-prices the trades the first run already made.

**Command, exactly as §11 prescribes:**

```
python scripts/candidate_study.py --window dev --cost-sweep --json docs/evidence/candidates_dev_2026_11.json
```

**Evidence:** `docs/evidence/candidates_dev_2026_11.json` (the machine record the holdout gate
reads) and `docs/evidence/candidates_dev_2026_11.txt` (the console transcript, byte-for-byte what
the run printed). Re-running the same command reproduces every number below; 39 tests in
`tests/test_candidate_study.py` pass.

## The result in one line

All three candidates **FAIL development**. No holdout was opened. Per §9 that is the deliverable:
*no edge of the detectable size in these three families on the 2004–2019 daily data.*

## Development, measured

| Candidate | n | clusters | win% | mean net R | 98.33% interval | detectable R | double-cost R | verdict |
|---|---|---|---|---|---|---|---|---|
| C1 inside-bar compression breakout (XAUUSD) | 221 | 164 | 49.8% | **+0.0233** | [−0.1291, +0.1749] | 0.186 | −0.0570 | FAIL |
| C2 gold/silver ratio reversion (XAUUSD) | 157 | 100 | 38.9% | **−0.1332** | [−0.2950, +0.0367] | 0.182 | −0.1994 | FAIL |
| C3 FX monthly time-series momentum | 540 | 180 | 38.3% | **−0.0983** | [−0.2320, +0.0389] | 0.209 | −0.1669 | FAIL |

Every candidate clears §8's sample floors (`n ≥ 100`, `clusters ≥ 24`), so these are real
failures to find an edge, not failures to gather evidence. All three intervals straddle zero, and
all three go negative at double cost — the second condition of §8.2 would have failed them even
if an interval had cleared.

## Cost sweep (§6, four re-pricings of the same trades)

| Candidate | zero cost | transaction only | base | double |
|---|---|---|---|---|
| C1 | +0.1036 | +0.0581 | +0.0233 | −0.0570 |
| C2 | −0.0670 | −0.1136 | −0.1332 | −0.1994 |
| C3 | −0.0297 | −0.0436 | −0.0983 | −0.1669 |

The mean cost per trade in R, as the engine reports it: **+0.080 R** for C1, **+0.066 R** for C2,
**+0.069 R** for C3. This is the sentence the design was built to make possible: C1's entire
gross edge is +0.104 R before any charge, and the charges registered in §6 take 0.080 R of it.
C2 and C3 are negative even at zero cost, so for those two the verdict is not a cost artefact —
the rules themselves do not predict.

## The confounds §5 predicted, measured

- **C1 — "a pass carried by the long side alone is a drift result."** Longs: n=127, +0.139 R.
  Shorts: n=94, −0.133 R. The candidate's positive mean is entirely gold's 2004–2019 uptrend, and
  the short half is where the compression idea actually lost. Had the long side alone been tested,
  it would still have failed its interval at 0.186 R detectable — but the pre-registered split is
  what makes this legible as drift rather than signal.
- **C2 — "a positive result carried by longs is gold beta."** Not reached: both halves lose
  (long −0.093 R on 75 trades, short −0.170 R on 82). The ratio dislocation does not revert inside
  five days on this data.
- **C3 — "all three pairs share a dollar factor."** Per symbol: EURUSD −0.158 R, AUDUSD −0.077 R,
  NZDUSD −0.060 R (180 trades each). Two of three are individually negative; the cluster bootstrap
  is what stops the shared dollar exposure from being counted as 540 independent observations.

## Exit anatomy (why the R distribution looks like this)

- C1: 130 of 221 exited on the 10-day time exit, 71 on the stop, only 19 reached the 2R target.
- C2: 117 of 157 exited on the 5-day time exit, 37 stopped, **2** reached target.
- C3: 277 stopped (plus 2 gap-through stops), 173 closed at the monthly rebalance, 88 reached 2R.

A 2R target needs to be hit more than one third of the time to pay for a stop-out. C2 hits it on
1.3% of trades and C3 on 16%: with those hit rates the mean can only be negative unless the
time-exit legs are strongly positive, and on this sample they are not.

## Holdout status

Not opened, and not openable. `scripts/candidate_study.py --candidate c1_inside_breakout --window
holdout` prints:

```
c1_inside_breakout has no passed development run recorded; the holdout stays closed
```

The gate reads the recorded n, clusters, mean and lower bound from
`docs/evidence/candidates_dev_2026_11.json` and re-derives them from the engine and the cache;
a candidate without `passed: true` there is refused before any holdout bar is read. §8 is
therefore closed as designed: three fails, zero holdout looks, no second chance at the same
rules with a different parameter.

## What this changes in the trading programme

1. **No rule in this repository has ever measured a cost-surviving edge.** Prior studies found
   none on gold, FX, indices or crypto; the superseded gold study's six families are now backed by
   these three daily candidates. Concretely, for the XAUUSD leg of that earlier study the cost
   constant was checked against the live broker book (spread measured 2026-11-02: 0.37 USD = 37
   points = 0.0095% of price, so the §6-style round trip is ≈0.0011 rather than the registered
   0.002) and re-priced at 0.002 / 0.0011 / 0.001 / 0. That re-pricing is **not** part of either
   protocol — it is a post-hoc sensitivity, recorded here only to answer "did the cost model kill
   a real edge". It did not: even at zero cost the best of those six families reaches +0.26 R with
   an interval that still contains zero at the registered confidence.
2. **The all-universe demo run is an execution-integrity test, not a search for profit** — exactly
   what §10 and the gold protocol always said. It is now also the only thing the account permits:
   the owner trades 114929502 himself.
3. **The next honest step is a new pre-registration, not a re-run of this one.** Any rule that
   survives 2004–2019 daily bars at double cost with an interval above zero would have to be a
   different family than these three; adding a fourth rule to this document after seeing its
   result is prohibited by §8.6 and would not be believed.

## Reviews

Three independent result critics reviewed this run and this document; their findings and how they
were disposed of are in `docs/studies/reviews/result/`.
