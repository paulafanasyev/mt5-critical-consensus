# Round 1 design reviews — summary and disposition

Three independent critics reviewed `docs/studies/prereg_v2_2026_11.md`,
`scripts/candidate_study.py` and `tests/test_candidate_study.py` on 2026-11-01 before any
development run. Each had a fresh context and was told not to execute the study. All three
returned **FIX_FIRST**. This file is the orchestrator's record of what they found and what was
changed in response; the critics' full texts were truncated in transport, so the findings are
recorded here in substance rather than verbatim, and round 2 (per-candidate files in this
directory) carries complete reviews of the revised design.

## Blockers raised

1. **The time exit was specified one way and implemented another** (C1). The prose read "the
   10th bar after entry, exit at the next open"; the code protects `E..E+9` and fills at
   `E+10`. No test pinned the boundary, and the parity implementation hard-coded the same
   choice, so agreement could not detect it.
2. **The burned-history claim was overstated.** The audit said "no artefact reads a daily bar
   before 2020-08-28", but the availability probes (`agent_data/_probe_*.py`, 15:10–15:18) had
   read bar counts, date ranges and spread medians over 2004–2020, and the test suite had
   executed the development rules at 15:22, before the stamp. Two audit rows were also wrong:
   `gold_ema50200_folds.json` is 8 × 1 175 H1 bars with a 600-bar warm-up, not "PAXG H1 full
   span", and `matrix2.json` records 3 750 cycles, not 10 000 bars.
3. **Re-entry after an exit was unspecified** (C1, C2). The engine resumes at `exit_index+1`,
   which discards a signal on the exit bar; two readers could legitimately disagree, changing
   n by ~15% for C2.
4. **The cluster bootstrap was a no-op for C1 and C2.** The cluster was the entry date and the
   one-position rule makes every entry date unique, so every cluster was a singleton: the
   interval was an i.i.d. trade bootstrap, narrower than the design claimed.
5. **The 2020–2023 "unused" buffer was used as indicator lookback** at the window edges (C3's
   252-day return on the first holdout month reaches into 2023; C2's warm-up likewise).
6. **C3 had no end-of-window clause** even though the code force-closes the final trade at the
   last close, so a partial month was scored as a full month and an open position as closed.
7. **The bootstrap ignored serial dependence** across months (C3), which was the one defect
   that could manufacture a false pass.

## Majors raised

- A bar that opened through the target and later traded through the stop was booked as a stop
  loss — a loss that could not occur.
- The "second implementation" in the tests was a transcription (it imported the engine's ATR,
  reused its constants, and compared only dates and prices), so it could not catch a shared
  misreading; it was also edited to agree with the engine.
- The multiplicity arithmetic was wrong: a two-sided 98.33% interval is a one-sided 0.83% test
  per candidate (family-wise ≈ 2.5%), not 1.67%; and the power figures used z = 2.13 instead of
  2.394 and trade counts instead of clusters.
- No direction split, so a long-only drift on a rising gold market could pass as a compression
  edge.
- No economic floor: the cost sweep was diagnostic, so an edge smaller than the cost model's
  uncertainty could pass.
- The holdout gate was advisory: `--window holdout` opened a lock for any candidate without
  checking that development passed, and `build(..., "holdout")` bypassed it.
- The holdout was materially less powered than development (30 trades at 90% detects ≈ 0.36 R)
  and the document did not say so.
- C2 was labelled a "lead-lag" but implements ratio reversion; no lag coefficient is estimated.
- The cost model is era-invariant (a 2026 fixed-dollar floor applied to 2004–2019) and the
  financing claim was backwards for metals and for AUD/NZD carry.

## Minors raised

Warm-up defined on the series index rather than the window; ATR is a partial mean before bar
14; a vacuous warm-up assertion; a 252 vs 253 off-by-one between engine and parity; truncation
test cannot detect within-window look-ahead; a zero-night trade at the window edge;
`profit_factor` could serialise as `Infinity`; `t` ignored clustering; `passed: false`
conflated "underpowered" with "failed"; no minimum cluster count; `pytest` executes the
development simulation; the review directory did not exist; the spread field is zero on a
third of bars; the XAGUSD cost row implied a leg that is never traded.

## Disposition

| Finding | Change |
|---|---|
| 1 | §5 now states "entry bar is held bar 1; protections on `E..E+H−1`; fill at `E+H`", and `test_the_time_exit_lands_on_the_registered_bar` pins the exact date |
| 2 | §2 rewritten: the claim is scoped to "no pre-2020 daily price path for any trading rule", the probes and the BTCUSD walk-forward are listed, the pre-run history is tabulated in §12, and both misstated rows are corrected |
| 3 | §5 states the rule explicitly; `test_no_signal_is_taken_on_the_bar_a_position_closes` pins it |
| 4, 7 | Clusters are entry months (identical for all three candidates) and the bootstrap is a moving-block bootstrap of three consecutive months; §7 states what it still cannot capture |
| 5 | §4 relabels the buffer "no trade of its own" and says it may be read backwards by a lookback |
| 6 | The common exit geometry now includes the end-of-window rule for all three candidates |
| gap-through-target | Both opens are checked before any intrabar level; a dedicated test |
| parity | Three implementations rewritten from the document, with their own ATR, constants and cost arithmetic, comparing direction, reason and R |
| statistics | §7 corrected to 0.83% one-sided / ≤2.5% family-wise and recomputed in cluster units with z = 2.394; `MIN_CLUSTERS = 30` added |
| drift | `by_direction` reported with every result; the confound is stated in §5 C1 |
| economic floor | A pass now also requires a positive mean at double cost |
| holdout gate | `--window holdout` and `build(..., "holdout")` both require a development-evidence file recording `passed: true`, and the lock stores that file's sha256 |
| holdout power | §8 states the ≈0.36 R detection threshold and that a smaller-sample failure is inconclusive |
| C2 naming | Renamed to gold/silver ratio reversion throughout, including the candidate id |
| costs | §6 states the era assumption, the direction-specific bias and what is not modelled; XAGUSD marked signal-only |
| minors | Warm-up test fixed, 252/253 aligned, `profit_factor` nullable, `t` removed, `status` added, review directory created, §11 notes the suite executes the simulation |
