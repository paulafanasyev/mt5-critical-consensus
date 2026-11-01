VERDICT: FIX_FIRST

Scope note: this is a read-only review. I did not execute `scripts/candidate_study.py` or
`pytest`, and I computed no return, signal or trade. I did verify the five cache sha256s and
the manifest sha256 against §3 with `Get-FileHash` (all match), and I counted joint XAU/XAG
dates from the `d` field only, to test the C2 calendar claim. Findings cite `path:line`.

## Round-1 findings

| # | Round-1 finding | Status | Evidence in the current files |
|---|---|---|---|
| B1 | Time exit specified one way, implemented another (C1) | **FIXED** | `docs/studies/prereg_v2_2026_11.md:121-124` now states "entry bar is held bar 1; protections on E..E+H-1; fill at E+H". `scripts/candidate_study.py:299-307` computes `time_exit = index+1+max_hold`, protects through `time_exit-1`, fills at `time_exit`. `tests/test_candidate_study.py:277-288` pins the exact date and 3 financing nights. The boundary is now the registered one. |
| B2 | Burned-history claim overstated | **PARTIAL** | The specific bad rows are corrected: `matrix2.json` really is 3,750 cycles and `market_matrix.json` 4,750 (verified from the JSON metadata), and `gold_ema50200_folds.json` really is 8 folds. But `prereg:27-30` still says "No artefact in this repository has examined a pre-2020 daily price path for any trading rule" and "nothing else touches the period", while `prereg:298-300` and `prereg:313` admit the test suite executes the development rules on 2004-2019, and `tests/test_candidate_study.py:415-422` runs `study.simulate(..., study.WINDOWS["dev"])` for C2 while `tests:451-463` asserts a non-empty C2 trade list. The §2 table (`prereg:32-43`) has no row for `scripts/candidate_study.py` or the test suite. §2 also makes a false arithmetic claim: floors are "four to ten times those medians" (`prereg:49-50`), but EURUSD floor 2 vs stated median 3 is 0.67x and NZDUSD 2 vs 2 is 1.0x (`prereg:47-48`, `prereg:187-189`). |
| B3 | Re-entry after an exit unspecified (C1, C2) | **FIXED** | `prereg:100-102` states the rule; `scripts/candidate_study.py:312-314` resumes at `exit_index+1`; `tests:291-302` pins that a signal on the exit bar is ignored. |
| B4 | Cluster bootstrap was a no-op (C1, C2) | **FIXED** | Clusters are entry months (`scripts/candidate_study.py:188`, `372-378`), the block is 3 months (`:51`, `382-394`), and `tests:509-526` proves the block mechanism and that clustering differs from i.i.d. resampling. See B7 and the residual-risk section for what the block still cannot do. |
| B5 | The 2020-2023 buffer was used as indicator lookback at window edges | **PARTIAL** | `prereg:90-94` now says the buffer may be read backwards and can never contribute a trade. C2 is defensible: the first holdout signal date is 2024-01-02 (in the holdout) and only the 60 prior gaps and the 5-step return are in 2023 (`scripts/candidate_study.py:231-240`). C3 is not: `simulate_monthly` sets `signal = entry_index-1` (`:324-333`), so the first holdout trade's signal bar is the last bar of December 2023, i.e. a buffer-formed decision, not a lookback. §4's parenthetical "the 65-observation warm-up of C2" (`prereg:93`) is also the wrong label: the 2008-2009 warm-up is at the series start; the 2023 read is a 65-observation lookback. |
| B6 | C3 had no end-of-window clause | **FIXED** | `prereg:118-124` and `:164-167` state the common end rule; `scripts/candidate_study.py:342-352` exits at `last_index = min(month_end, end)` with reason `end`; the C3 branch of `tests:466-489` exercises it. |
| B7 | The bootstrap ignored serial dependence across months (C3) | **PARTIAL** | The block bootstrap is real (`scripts/candidate_study.py:382-394`) and §7 discloses its limits (`prereg:220-225`). But the block is drawn over the months that appear in the trade list (`:372-375`); a month with no trade is not a cluster, so "three consecutive months" can join non-adjacent months. For C1 and sparse C2 months the advertised adjacency is therefore partly fictional. Three months is also short for C3's multi-year regimes, which the document admits rather than repairs. |
| M1 | A bar that opened through the target was booked as a stop | **FIXED** | `scripts/candidate_study.py:145-165` checks both opens before any intrabar level; `tests:230-235` pins the exact case. |
| M2 | The "second implementation" was a transcription | **PARTIAL** | It no longer imports the engine's ATR or constants and now compares direction, reason and R (`tests:405-433`). But `_tr_atr` (`tests:50-63`) is a line-for-line copy of `atr` (`scripts:110-125`) and `_fill` (`tests:75-96`) is a branch-for-branch copy of `protective_exit` (`scripts:145-165`); `_cost` (`tests:66-72`) is the same formula. A shared misreading of ATR or of the fill order passes every parity test. The test docstring's claim that "a shared mistake in ATR, cost or exit ordering cannot hide" (`tests:3-6`) is therefore false for ATR and for the fill order. |
| M3 | Multiplicity arithmetic was wrong | **FIXED** | `DEV_CONFIDENCE = 1 - 0.05/3 = 0.98333` (`scripts:46`); §7 now says 0.83% one-sided and at most 2.5% family-wise (`prereg:226-229`); `z = 2.394` matches a one-sided 0.83% normal tail. |
| M4 | No direction split | **PARTIAL** | `by_direction` is reported for every candidate (`scripts:427`, `446-451`), and C1 states the drift confound (`prereg:136-138`). But the decision rule does not require a two-sided edge: a long-only compression result is still recorded `passed: true`, with a caveat. C2 has no such caveat at all (see M-new-2). |
| M5 | No economic floor | **FIXED** | `verdict` now requires `double_cost_mean > 0` (`scripts:548`, `562-563`), `cost_sweep` re-prices the same trades at 0x, transaction-only, base and double (`:525-534`), and §8 step 2 registers it (`prereg:243-245`). The gate exists and binds; see M-new-3 for why a point estimate is still too weak for the claim in §6. |
| M6 | Holdout gate was advisory | **FIXED** (but see M-new-4) | `open_holdout` now requires `report["window"] == "dev"` and `decision["passed"]` (`scripts:497-500`); `require_holdout_open` checks the lock and the evidence hash (`:473-490`); `build` calls it for `holdout` (`:515-516`); `main` opens before scoring (`:577-580`); `tests:529-564` cover it. The specific round-1 hole is closed. |
| M7 | The holdout was less powered and the document did not say so | **FIXED** | §8 states the ≈0.36 R threshold and that a smaller-sample failure is inconclusive (`prereg:261-264`); `HOLDOUT_CONFIDENCE = 0.90`, `MIN_HOLDOUT_TRADES = 30` (`scripts:47-49`); `verdict` applies them (`:538-539`). The arithmetic (1.645 x 1.2 / sqrt(30) = 0.36) is right. |
| M8 | C2 was labelled "lead-lag" but implemented ratio reversion | **PARTIAL** | The candidate id and prose are renamed (`scripts:72`, `prereg:140-144`). But the rule computes a difference of 5-day *returns* (`scripts:223-230`), never the ratio `XAU/XAG` or its mean; the trade is outright XAUUSD exited at 2R, stop or 5 bars (`:108-119`, `299-307`), not at convergence. The function docstring's "back to zero" (`:213`) is false. The specific lead-lag mislabel is gone; "ratio reversion" is still not what is tested. |
| M9 | Cost model era-invariant and financing claim backwards | **PARTIAL** | §6 now states the era assumption, the aggressive 5 bp slippage and the direction-specific financing bias (`prereg:195-207`), and marks XAGUSD signal-only (`:188`). But `prereg:196-197` says the fixed-dollar floor is "relatively heavier early" and that "that direction flatters the early years": a fixed $0.20 floor is heavier in R terms when gold is cheap, so it *penalises* the early years; the sentence has the sign backwards. §2's "four to ten times" is also false for the FX floors (B2). |
| m1 | Warm-up defined on the series index, not the window | **PARTIAL** | Still true for C3 (`scripts:256`) and C2 (`:231`): C2's first eligible signal is 2009-02-11 because the joint series starts 2008-11-07, so the 2004-2009 part of the registered development window cannot trade. The test-side warm-up assertion was fixed, but the document still presents the full 2004-2019 window (see M-new-5). |
| m2 | ATR is a partial mean before bar 14 | **FIXED in effect** | `atr` still returns a shorter mean before bar 14 (`scripts:121-124`), but no signal can fire there: C1 starts at index 30, C2 at joint step 65, C3 at 252 (`:200`, `:231`, `:256`). Not a live defect. |
| m3 | Vacuous warm-up assertion | **PARTIAL** | `tests:379-386` asserts only `intent[:65] == 0` and does not create a date present in gold but absent from silver, so `common` is never compressed in that fixture and `gold_at == step` throughout. It cannot catch the alignment bug it is named for (see M-new-6). |
| m4 | 252 vs 253 off-by-one | **FIXED** | `signal < 252` in the engine (`scripts:256`) and the independent (`tests:205`); §5 says "at least 252 (0-based, so 253 bars)" (`prereg:160`). |
| m5 | Truncation test cannot detect within-window look-ahead | **PARTIAL** | `tests:466-489` only removes the future; a statistic computed over the whole window would be identical in both runs. It also compares only dates and R, not direction or reason. For C2, whose only look-ahead test is this one, a within-window leak would pass. |
| m6 | A zero-night trade at the window edge | **NOT FIXED** | A signal on the penultimate bar enters at the last bar's open and is force-closed at that bar's close with `nights = 0` (`scripts:288-307`, `176-177`). §5 registers the close-out but not the same-bar entry, and the zero financing is a small undercharge. |
| m7 | `profit_factor` could serialise as `Infinity` | **FIXED** | Returns `None` when `loss_total == 0` (`scripts:421`). |
| m8 | `t` ignored clustering | **FIXED** | The `t` statistic is gone; the interval is the cluster bootstrap (`scripts:363-397`). |
| m9 | `passed:false` conflated underpowered with failed | **PARTIAL** | `verdict` now returns `status: no_decision` vs `fail` (`scripts:540-565`), but §9 still says "a candidate that fails development" for the no_decision case (`prereg:266-275`). |
| m10 | No minimum cluster count | **FIXED** (24 vs 30 seam) | `MIN_CLUSTERS = 30` (`scripts:50`), enforced at `:544-547`, tested at `tests:574-578`. But §8 registers 24, not 30 (B-new-1). |
| m11 | pytest executes the development simulation | **PARTIAL** (disclosed) | §11 and §12 disclose it (`prereg:298-300`, `:313`), but the suite still runs `study.simulate` on `WINDOWS["dev"]` (`tests:405-433`, `452-489`), and a failed assertion prints the compared trade lists. Disclosure is not prevention. |
| m12 | The review directory did not exist | **FIXED** with a gap | The directory exists and holds `00_round1_summary.md`, but §12 claims "three independent critics, one per candidate, plus the fix log" (`prereg:320-321`); only the summary exists. |
| m13 | Spread field zero on a third of bars; XAGUSD row implied a leg | **FIXED** | Disclosed at `prereg:47-51` and `:193-196`; XAGUSD is explicitly "signal only, never traded" (`:188`). |

## New findings

### Blockers

**B-new-1. The registered cluster floor is 24; the code enforces 30, and the evidence will
record 30.** `prereg:243-245` and `prereg:252-254` say `clusters >= 24`; `prereg:230` says 30;
`scripts/candidate_study.py:50` sets `MIN_CLUSTERS = 30`; `verdict` rejects anything below 30
(`:544-547`) and writes `threshold_clusters: 30` into the evidence (`:564-565`). This is the
same class of defect round 1 caught in the time exit: the binding rule in the document is not
the rule in the code. A C2 with 25-29 entry months would be a pass under §8 and a
`no_decision` under the code, and the result document would cite a threshold the
pre-registration does not contain. It is stricter, so it cannot manufacture a pass; it can
silently discard one and it invalidates the claim that the stamped document *is* the executed
analysis. Fix: pick one number, put it in §7, §8.2 and §8.4 and `MIN_CLUSTERS`, and re-stamp.

**B-new-2. Section 2 still states something false, in the one section whose job is to be
literally true.** The "precise claim" (`prereg:27-30`) is an absolute: "No artefact in this
repository has examined a pre-2020 daily price path for any trading rule." The test suite is
an artefact, and `tests:405-433` and `:451-489` execute all three rules over
`WINDOWS["dev"]` (2004-2019), including C2's full joint-gap signal and trade list; §11 and §12
admit this. The table that follows has no row for `scripts/candidate_study.py` or
`tests/test_candidate_study.py`. The same section contains a numerical falsehood ("floors are
four to ten times those medians", false for EURUSD and NZDUSD) and a sign error in the era
direction (`prereg:196-197`). The honest version is: "The engine and its test suite have
executed the rules over 2004-2019; they print no statistic, but the trade lists have been
computed. No performance number has been seen. The 2004-2019 daily path has not been examined
for any other rule family, except crypto H1." That is a different, defensible claim. Until it
is written, a reader can be told the development window is untouched when it is not.

### Majors

**M-new-1. C2 does not trade the ratio it is named after.** `rule_c2` computes
`g = gold 5-day return - silver 5-day return` (`scripts:223-230`) and z-scores it against the
trailing 60 gaps (`:231-240`); it never forms `XAU/XAG`, never estimates a ratio mean, and
never exits on convergence. The trade is outright XAUUSD with a 2R target, a 1R stop and a
5-bar time exit (`:108-119`, `:299-307`). So a pass supports "a 5-day relative-return
dislocation predicts gold's direction over the next week", not "the gold/silver ratio
reverts". The docstring's "back to zero" (`:213`) is false: the 2R target or the 5-bar clock
can close the trade while the gap is still wide, and a 2R target is not a convergence target.
Because the position is outright gold, the P&L is gold beta plus a small relative signal, not
a spread. Fix: rename the candidate to gold/silver relative-return reversal (or add a
convergence target and a ratio leg if the ratio claim is intended) and state in §5 that a pass
is a directional forecast, not evidence of ratio mean reversion.

**M-new-2. C2 has no gold-drift confound, although it is an outright gold rule over a period
when gold rose about fourfold.** C1 states the confound and requires the reader to check the
long/short split (`prereg:136-138`). C2's section (`prereg:140-156`) has no such paragraph,
even though `by_direction` (`scripts:427`, `446-451`) will show it. A C2 pass carried by the
long side — long gold after it lagged silver, in a bull market — would be recorded `passed:
true` and read as reversion. §8 does not require the short side, or a drift-adjusted benchmark,
to be non-negative. Fix: add C1's caveat verbatim to C2 and make the verdict text name a
one-sided pass as a drift result.

**M-new-3. The double-cost gate is a point estimate on the same trades, so the §6 claim that
"a result that lives inside the uncertainty of the cost model cannot pass" is not
established.** `verdict` only checks `double_cost_mean > 0` (`scripts:548`, `562-563`); the
double-cost trades are the base trades re-priced (`:525-534`), with no interval and no
additional sampling evidence. A candidate with base mean +0.30 R and double-cost mean +0.001 R
passes, although its double-cost interval almost certainly straddles zero. Doubling both
transaction and financing is a sensible stress, but it is one point on a cost curve, not the
model's uncertainty; the floor provenance itself (§2 caveat 1) is an uncertainty the sweep
does not touch. Fix: either require the double-cost *lower bound* to clear zero, or require
the base lower bound to exceed the double-cost increment, and rewrite §6 to say "survives a 2x
cost stress", not "lives inside the uncertainty".

**M-new-4. The holdout gate is a guardrail, not a seal, and §8 claims the latter.**
`open_holdout` trusts a plain JSON field `decision["passed"]` (`scripts:497-500`); nothing
authenticates the evidence file, and `tests:537-543` demonstrates the bypass by writing such a
file by hand. `simulate`, `simulate_monthly` and `summarise` are public and can be called on
`WINDOWS["holdout"]` without `build` (`:277-357`, `:400-428`); `build` is only the convenience
entry point that checks the lock (`:515-516`). `main` writes the lock before it scores
(`:580`), so a crash or a typo burns the one look with no result file, and a holdout run
without `--json` records only terminal output. §8's "the tool refuses otherwise ... deleting
the document is the only way around it, and that deletion is visible" (`prereg:246-251`) is
false: writing a fake document is easier than deleting one and leaves the lock hash intact.
Fix: say plainly that the gate prevents accidental premature opening and relies on the
operator's honesty for the rest; if a harder seal is wanted, make a single run entry point and
record the run in a file that is itself hashed into the lock.

**M-new-5. C2's effective development window is 2009-02-11 to 2019-12-31, not 2004-06-11, and
the document does not say so.** XAGUSD starts 2008-11-07 (`prereg:74`), the first joint date is
2008-11-07, and `rule_c2` requires joint step 65 (`scripts:232`), so the first possible signal
is 2009-02-11. From the cache's `d` fields I counted 2,862 joint dates in the development
window and 134 joint months; with the warm-up, at most about 130 entry months are available.
The §7 power table is generic (`prereg:230-237`) and never says C2 loses 4.6 of its 15.5
years. At ~130 clusters the detectable edge is about 0.26 R, not the 0.29 R quoted for 100
clusters, and a true 0.25 R edge has roughly 50% development power. Fix: state the effective
window and C2's cluster count before the run.

**M-new-6. The C2 unit test that is supposed to pin joint-date alignment is vacuous.**
`tests:379-386` pops gold index 40, but every remaining gold date is still present in silver,
so the joint list is not compressed and `gold_at == step` for the whole fixture; the only
assertion is that no signal fires before index 65. A bug that indexed the return by `step-5`
instead of `common[step-5][0]` would pass this test. The real-cache parity (`tests:415-422`)
does exercise compression, but only incidentally: after 2008-11-07 the calendars differ on
just 12 development dates (6 gold-only: 2009-04-03, 2009-10-01, 2010-03-22, 2010-10-08,
2011-07-01, 2015-04-03; 6 silver-only), and only 30 of 2,857 development steps have
`joint-5 != gold-5`. Fix: add a fixture with a date present in gold but absent in silver and
assert the exact gap and the exact signal step.

**M-new-7. The C2 z-score's numerator overlaps its denominator, so "1.5-sigma" is not a 1.5
sigma event.** `gaps[k]` spans joint steps k-5..k; the 60-gap window `gaps[k-60:k]` includes
`gaps[k-1]..gaps[k-4]`, which share 4 of those 5 days (`scripts:228-240`). A large dislocation
therefore inflates `s` as well as `g`, damping exactly the z the rule is trying to detect. The
prereg discloses that the 60 gaps are overlapping (`prereg:154-156`) but not that the current
gap is partly inside the scale estimate. The practical effect is fewer signals at the largest
dislocations and a trigger rate that is not the normal 1.5-sigma rate. Fix: state the overlap
in §5 and either exclude the overlapping gaps from the scale or describe 1.5 as a tuned
threshold, not a probability.

**M-new-8. C3's first holdout trade is decided by a buffer bar.** `simulate_monthly` uses
`signal = entry_index - 1` (`scripts:332-333`); for the January 2024 entry that is the last bar
of December 2023, inside the excluded 2020-2023 buffer. §4 says the buffer "can never
contribute a trade" (`prereg:90-94`), but it contributes the direction of the first holdout
trade. §4's "the 252-day return on the first holdout month" is also mislabelled: the return is
computed on the last day of the *prior* month. C2 does not have this problem — its first
holdout signal bar is 2024-01-02, and only the lookback is in 2023 — so the two candidates are
not treated alike. Fix: drop the first holdout trade for C3, or state that its signal is
buffer-formed and report it separately.

### Minors

- **ATR is untested.** `tests:50-63` is a copy of `scripts:110-125`, and every synthetic bar
  in the fill tests has `h-l = 0.02v` and `c = v`, so ATR is constant and insensitive to the
  period or the TR convention. A wrong ATR shifts every R by a constant factor and passes all
  parity and unit tests. Add one bar with a gap and assert a known ATR.
- **Financing counts bar-index nights, not calendar nights** (`scripts:176-177`): a Friday to
  Monday hold is one night, not three. For C2's 5-bar hold the model charges 5 nights where
  the calendar has about 7, an undercharge of roughly 0.01 R on gold; small, but the double
  cost stress inherits it.
- **The §6 era sentence has the sign backwards** (`prereg:196-197`): a fixed $0.20 floor is
  heavier in R terms when gold is $400 than at $1,600, so it penalises the early sample; it
  does not "flatter" it.
- **Documentation counts.** §11 says "36 tests" (`prereg:291`); the file has 29 test functions,
  three of them parametrised 5+3+3, so pytest collects 37. §11 lists `WARMUP_C1` and
  `WARMUP_C3` but C2's 60 and 65 are magic numbers in `rule_c2` (`scripts:231-234`); §8.1 is
  cited at `prereg:11` but §8 has no subsection.
- **The holdout run does not force a record.** `main --window holdout` writes the lock
  (`scripts:580`) but only writes a result file if `--json` is passed (`:610-613`); a burned
  holdout can leave no evidence file. `require_holdout_open` also does not check
  `report["window"] == "dev"` or that the recorded `cache_manifest_sha256` equals the current
  manifest (`:473-490`), so a stale-cache or wrong-window evidence file can open the holdout.
- **The audit table merges two artefacts.** `prereg:34` describes `market_matrix.json` and
  `matrix2.json` together as "SMA, EMA, Bollinger and Donchian on EURUSD, GBPUSD, AUDUSD,
  NZDUSD, XAUUSD, XAGUSD, US500". In fact `market_matrix.json` has five symbols and four
  rules, and `matrix2.json` has seven symbols but only EMA and Donchian (verified from the
  metadata). The merged row is defensible as a union but reads as if both ran everything.
- **§2's C2 holdout answer is instrument-blind** (`prereg:60`): "No. No cross-asset rule
  exists anywhere" is true for the rule, but the gold path 2024-2026 was examined by
  `gold_research_report.txt` and `calendar_gold.txt`, and the silver path by `matrix2.json`.
  §2:63-64 discloses the general "rule, not period" point, but the row's bare "No" invites the
  opposite reading.

## What the design gets right

- **The exit geometry is genuinely unambiguous now.** The two opens are checked before any
  intrabar level, the stop wins inside the bar, and the ordering is pinned by four hand-built
  tests (`scripts:145-165`, `tests:230-259`). Round 1's "a loss that could not occur" is gone.
- **The C2 joint alignment is correct, and it is not a no-op.** `rule_c2` uses
  `common[step-5][0]` for gold and `common[step-5][1]` for silver (`scripts:228-230`), so both
  legs span the same pair of joint dates; the first gap at joint step 5 and the first signal at
  step 65 are exactly the registered 5-observation horizon and 60-gap warm-up
  (`scripts:231-240`). My date-only check confirms the calendars really do differ (6 gold-only
  and 6 silver-only development dates after 2008-11-07), so the alignment code is exercised,
  not decorative.
- **The standardisation is stated honestly.** The 60 overlapping five-day gaps are called out
  as roughly twelve independent blocks (`prereg:154-156`), the n-1 sample sd is explicit, and
  the `s <= 0` guard is in code (`scripts:237-239`).
- **The time exit, the re-entry rule and the end-of-window rule are each specified in prose,
  implemented once, and pinned by a date-level test** (`prereg:100-124`, `scripts:299-314`,
  `tests:277-302`). These were the three places a reader could legitimately disagree in round
  1; they are now closed.
- **The statistics are arithmetically right as far as they go.** 0.9833 two-sided = 0.83% per
  tail, at most 2.5% family-wise, z = 2.394, power stated in cluster units, holdout power
  stated before the look (`prereg:226-237`, `:261-264`). The remaining statistical objections
  are about what a percentile bootstrap with 10-40 blocks can deliver, not about the
  arithmetic.
- **The data are frozen and verifiable.** I recomputed all five file hashes and the manifest
  hash; they match §3 and the manifest (file `73d4359f...`). The engine never connects to the
  terminal, and the study reads only the cache (`scripts:85-93`).
- **The document is candid about most of its weaknesses** — overlapping gaps, gold drift for
  C1, era-invariant costs, direction-blind financing, holdout power, and the crypto-only
  pre-2020 path. The remaining overclaims are specific and fixable, not a pattern of hiding.

## Residual risk if run as written

- **C2 will probably be underpowered, not fooled.** With roughly 130 entry months and a
  6-bar minimum cycle over 2,862 joint bars, the trade count should clear 100 comfortably but
  is unlikely to approach 450; the detectable net edge is about 0.25-0.26 R. A true 0.25 R edge has roughly 50%
  development power, and the 30-trade holdout adds roughly 40%; the most likely outcome is
  `no_decision` or "inconclusive", not a false pass. That is the honest reading of the design's
  own power table, but the document should say it for C2 specifically.
- **A long-only gold-beta result can still be reported as ratio reversion.** The direction
  split will be printed, but no rule requires the short side to be non-negative, and C2 has no
  drift caveat. On gold 2009-2019 this is the single most plausible way a "reversion" pass is
  actually a trend pass.
- **The double-cost gate can pass an edge that is indistinguishable from zero at double cost.**
  A base mean of +0.30 R with a double-cost mean of +0.001 R passes, because the gate checks a
  point, not a lower bound.
- **The 0.83% / 2.5% family-wise claim is nominal, not verified.** At the §7 floor of 30
  clusters the block bootstrap has ~10 blocks, far too few to estimate a 0.83% tail; with
  sparse months the "consecutive months" are not consecutive. C2's ~130 clusters are much
  better, but the design's own admission that a multi-year regime is not captured means the
  true false-pass rate is above 2.5% in a trending market.
- **The one holdout look can be burned or bypassed.** The evidence JSON is unauthenticated,
  `simulate` can be called on the holdout window without `build`, and the lock is written
  before the score. If any of that happens, the burned holdout can still be presented as
  clean, because only the operator sees the record.
- **This is out-of-sample for the rules, not for the periods or the instruments.** The gold
  and silver paths 2024-2026 were examined by earlier artefacts, as §2:63-64 says; C2 trades
  outright gold, so its holdout is closer to a rule-OOS test on a partly-seen instrument than
  the table's "No" suggests.
- **C3's first holdout trade is decided by a buffer bar** (`scripts:332-333`), and C2's first
  holdout signal uses 2023 data for its scale and ATR (declared). The edge trades are a small
  fraction of each holdout sample, but they are not clean in the same sense as the rest.
