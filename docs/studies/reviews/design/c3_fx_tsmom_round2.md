VERDICT: FIX_FIRST

# C3 — monthly time-series momentum (EURUSD / AUDUSD / NZDUSD): round-2 design review

Scope: `docs/studies/prereg_v2_2026_11.md` (revised), `scripts/candidate_study.py`,
`tests/test_candidate_study.py`, `docs/studies/reviews/design/00_round1_summary.md`,
`docs/evidence/daily_cache_manifest.json`, plus the artefacts the burned-history audit cites.
I did not execute `scripts/candidate_study.py` or `pytest`. I did hash the two manifest files
(byte-identical, sha256 `73d4359f…`, so the code's `manifest_hash()` reproduces §3's recorded
value) and I inspected cache ranges, JSON keys and the pytest cache. Nothing here computes a
trade.

The candidate is in better shape than the rejected design. It still cannot be run as written:
one clause conflict changes every C3 entry price, the holdout lock is forgeable and repeatable,
and the 37-test verification has never executed in its current form.

## Round-1 findings

| # | Round-1 finding | Verdict | Evidence |
|---|---|---|---|
| 1 | Time exit specified one way, implemented another (C1) | **FIXED** | §5:121-124 now says "entry bar E is held bar 1 … protections live on bars E…E+H−1 … closed at the open of bar E+H". `simulate` implements exactly that: `time_exit = index + 1 + max_hold`, protective scan capped at `time_exit - 1` (candidate_study.py:299-307). `test_the_time_exit_lands_on_the_registered_bar` pins the date (test:277-288), and the independent C1 uses the same boundary (test:132-136). |
| 2 | Burned-history claim overstated; two audit rows wrong | **PARTIAL** | The two wrong rows are corrected: `matrix2.json` does record `cycles: 3750`, and `gold_ema50200_folds.json` is 8 folds × 1 175 cycles (verified). But the bold claim "No artefact in this repository has examined a pre-2020 daily price path for any trading rule" (prereg:27-30) is false of the repository's own suite: tests:405-433 and :451-489 run `build`/`simulate` on the real 2004–2019 cache and compare R values; `.pytest_cache/v/cache/nodeids` shows a session at 15:22:29. And prereg:3 ("Development has not been run") contradicts prereg:297-300 ("the suite executes the development simulation"). |
| 3 | Re-entry after an exit unspecified (C1, C2) | **PARTIAL** | Fixed for C1/C2: §5:101-103 states the rule, `simulate` resumes at `exit_index + 1` (candidate_study.py:314), and test:291-302 pins it. Not fixed for C3: the same open is both the exit and the next entry (see B1), the common clause forbids it, and no test exercises `simulate_monthly` for it. |
| 4 | Cluster bootstrap a no-op for C1/C2 (singleton clusters) | **FIXED** | The cluster is now the entry month (`month_key`, candidate_study.py:168-169; set at :188), and the bootstrap groups by it (:372-377). `test_trades_cluster_by_entry_month` (test:353-361) and `test_clustering_is_not_the_same_as_treating_trades_as_independent` (test:518-526) both pin the change. |
| 5 | 2020–2023 "unused" buffer was used as indicator lookback | **FIXED** | §4:93 relabels it and states the read. The code is consistent: no buffer entry is possible (`entry_index > end` breaks at the first 2020 month start, candidate_study.py:328-330), and only the 2023-12 signal of the first holdout month is read (:332-333). |
| 6 | C3 had no end-of-window clause although the code force-closes | **FIXED** | §5:118-119 and :166-167 now cover all three candidates; the code exits at the last close with reason `end` (candidate_study.py:351-352) and reports the reason histogram (:431-435). |
| 7 | Bootstrap ignored serial dependence across months (C3) | **PARTIAL** | A moving-block bootstrap of three months with wrap-around exists (candidate_study.py:51, :388-394) and §7:217-225 states the >3-month limitation. But the block is 3 months against a 252-day (12-month) signal; the fix narrows the gap, it does not close it (see M1). |
| 8 | A gap through the target later booked as a stop loss | **FIXED** | Both open checks precede any intrabar level (candidate_study.py:145-165), with `test_a_gap_through_the_target_is_not_later_rebooked_as_a_stop` (test:230-235) and the mirror (test:252-259). |
| 9 | "Second implementation" was a transcription, edited to agree | **PARTIAL** | It now has separate constants (test:45-47), its own cost function (:66-72) and compares reason+R (:99-105, :405-433). But `_tr_atr` (:50-63) reproduces `atr` (candidate_study.py:110-125) branch for branch and `_fill` (:75-96) reproduces `protective_exit` (:138-165) branch for branch; and `agent_data/_patch_tests7.py:4-6` shows the independent C1 warm-up edited from `<2` to `<30` at 15:22:25, after the engine's warm-up changed at 15:22:05. |
| 10 | Multiplicity arithmetic wrong | **FIXED** | `DEV_CONFIDENCE = 1 - 0.05/3` (candidate_study.py:46), §7:226-229, and the power table now uses z = 2.394 in cluster units (prereg:232-237). 0.83% one-sided per candidate ⇒ ≤2.5% family-wise is correct. |
| 11 | No direction split (gold drift could pass as a compression edge) | **FIXED** | `per_direction` (candidate_study.py:446-451), printed for every result (:606-607), and the C1 confound is stated at §5:136-138. |
| 12 | No economic floor (cost sweep was diagnostic) | **PARTIAL** | A double-cost gate exists (candidate_study.py:528-534, :548). But it is a point mean, only 2×, with no interval; and §6:211's claim that a result "inside the uncertainty of the cost model cannot pass" is too strong — a model that understates cost by 4× passes. For C3 the gate (~0.16 R of headroom) is weaker than the interval anyway. |
| 13 | Holdout gate was advisory | **PARTIAL** | `build(..., "holdout")` and `--window holdout` now both require the lock and a passed development document (candidate_study.py:473-490, :513-522, :577-580). But the document is trusted, not recomputed, and the lock does not consume the holdout — see B2. |
| 14 | Holdout less powered; document did not say so | **PARTIAL** | §8:261-264 now states ≈0.36 R and "inconclusive". But `verdict` has no inconclusive status (candidate_study.py:537-565), so the machine record cannot honour that sentence — see M2. |
| 15 | C2 labelled "lead-lag" but implemented ratio reversion | **FIXED** | §5:140-145 and the renamed id `c2_ratio_reversion` (candidate_study.py:72, :461). |
| 16 | Cost model era-invariant; financing claim backwards | **FIXED (as disclosure)** | §6:191-207 states the 2026 floor applied to 2004–2019, the direction-blind financing and what is not modelled. The bias is disclosed rather than corrected, which is the only implementable option without a carry series. |
| 17 | Warm-up defined on the series index, not the window | **NOT FIXED** | `rule_c1` loops `range(WARMUP_C1, count)` over the whole series (candidate_study.py:200) and `rule_c3` tests `signal < WARMUP_C3` on the series index (:256). For C3 this makes the first entry 2005-02 and leaves the declared 2004-06-11 edge empty; the 2004-01-01…2004-06-10 region is read as lookback but belongs to no declared window (see m4). |
| 18 | ATR is a partial mean before bar 14 | **NOT FIXED (inert)** | `atr` still averages a short window (candidate_study.py:121-125). It is unreachable at a signal because C1 needs ≥30 bars and C3 ≥252 (:200, :256), so it cannot change a trade; it is a latent trap for any future rule. |
| 19 | Vacuous warm-up assertion | **NOT FIXED** | test:375-376 asserts `intent[:30] == [0]*30` for a loop that literally starts at 30; the assertion restates the constant rather than testing the behaviour. Same for C2 at test:386. |
| 20 | 252 vs 253 off-by-one between engine and parity | **FIXED** | Both use 252 (candidate_study.py:256, test:205), matching §5:160-161's "at least 252 (0-based, so 253 bars of history)". |
| 21 | Truncation test cannot detect within-window look-ahead | **NOT FIXED** | test:467-489 truncates only at the window end, so any look-ahead that stays inside 2004–2019 is invisible to it. This is the test that is supposed to prove no look-ahead. |
| 22 | Zero-night trade at the window edge | **FIXED** | `nights = exit_index - entry_index` and a zero-night trade gets zero financing (candidate_study.py:176-177); `test_financing_grows_with_the_number_of_held_nights` covers the monotonicity (test:327-337). |
| 23 | `profit_factor` could serialise as `Infinity` | **FIXED** | Returns `None` when there are no losses (candidate_study.py:421). |
| 24 | `t` ignored clustering | **FIXED** | The `t` statistic is gone; only the cluster bootstrap remains (candidate_study.py:363-397). |
| 25 | `passed: false` conflated "underpowered" with "failed" | **FIXED** | `status` is `no_decision` vs `fail` vs `pass` (candidate_study.py:540-565), tested at test:567-578. |
| 26 | No minimum cluster count | **PARTIAL** | `MIN_CLUSTERS = 30` exists (candidate_study.py:50, :544), but the registered rule still says 24 twice (§8:243, :252) while §7:230 says 30 — see m1. |
| 27 | `pytest` executes the development simulation | **NOT FIXED** | Disclosed at prereg:297-300, but the tests still run `build`/`simulate` on the real cache for all three candidates (test:405-433, :451-489), and a failure prints the trade lists — the disclosure claims "cannot leak a result", which is wrong (see M5). |
| 28 | Review directory did not exist | **FIXED** | `docs/studies/reviews/design/` exists. |
| 29 | Spread field zero on a third of bars | **FIXED** | The floor dominates the measurement (candidate_study.py:130; §6:193-196), and the provenance of the floors is disclosed (§2:46-51). |
| 30 | XAGUSD cost row implied a leg that is never traded | **FIXED** | §6:188 marks it "signal only, never traded" and the candidate never trades it (candidate_study.py:462-463). |

## New findings

### Blockers

**B1. C3's rebalance re-entry is the exception the common rule forbids, and nothing pins it.**
§5:101-103 says, for all three candidates, "no new signal is evaluated on the bar on which a
position is closed — the next candidate signal is the bar after the exit, filled at the open of
the bar after that." C3 instead exits at the open of the first day of month M+1 and opens the
next position at that same open: `exit_index = next_start`, `exit_price = bars[next_start]["o"]`
(candidate_study.py:349-350), and the next loop iteration enters at `entry_index == next_start`
(:327-337). The C3 bullet (§5:163-165) calls the rebalance "a time exit, not a signal", which
is the intended escape, but it never says the new position opens on the exit bar. A reader
applying the common clause delays every C3 entry by one trading day (or skips a month), which
changes every entry price and therefore every R — a different trade list before any statistic
is computed. `test_no_signal_is_taken_on_the_bar_a_position_closes` (test:291-302) only exercises
`simulate` (C1/C2); `simulate_monthly` is never tested for it, and `_independent_c3`
(test:194-224) encodes the same same-bar re-entry, so parity cannot detect the choice.
Fix: one sentence in §5 C3 ("the new position opens at the same open at which the prior one
rebalances; the rebalance is not a signal, so the common re-entry clause does not apply") and a
test asserting two consecutive C3 trades share that date and price.

**B2. The holdout "exactly once" gate is forgeable and repeatable, and its audit field is null.**
`open_holdout` accepts any JSON that has `window == "dev"` and `decision.passed == true`
(candidate_study.py:493-510); it never recomputes the development result, never checks
`cache_manifest_sha256`, and never checks `n`, `clusters` or the double-cost mean. The suite
itself demonstrates the forgery: test:537-543 and :557-559 write a two-key document and the gate
opens. `require_holdout_open` (:473-490) only checks the file digest and `passed`. The lock is not
consumed, so `build(candidate, "holdout")` (:513-522) re-scores the holdout any number of times;
only `main` (:577-580) blocks a second CLI opening. The lock lives in `agent_data/`
(candidate_study.py:38), which is gitignored (`.gitignore`, last line `agent_data/`), so deleting
it — which re-opens the holdout — leaves no trace; §8:246-251's "deleting the document is the only
way around it, and that deletion is visible" is false on both counts. Finally
`locks[candidate]["development_low"] = decision.get("low")` (:507) reads a key that `verdict`
never returns (:537-565), so the lock always records `null`.
Consequence: a burned holdout can be presented as one clean look, which is the single thing this
protocol exists to prevent. Fix: recompute the development evidence from the cache (or store and
re-verify `n`, `clusters`, `low`, double-cost mean), consume the lock on the first scoring call,
and put the lock under `docs/evidence/`.

**B3. The 37-test verification has not executed in its current form.**
`scripts/candidate_study.py` was last written 15:37:03 and `tests/test_candidate_study.py` at
15:37:15. `.pytest_cache/v/cache/nodeids` was written at 15:37:18 (collection), but
`.pytest_cache/v/cache/lastfailed` still carries the 15:22:29 session's five unrelated failures,
so the last *executed* session predates the block-bootstrap, `MIN_CLUSTERS = 30` and parity
rewrites. The C3 parity test (test:425-433) — the only mechanical check that the engine and the
"independent" implementation agree on the month boundary — has never run. The first run that does
will also be the first run of the new bootstrap and the 30-cluster verdict. Fix: run the suite
once with output captured (or restructure the parity tests onto a synthetic price path so that
verifying the engine does not consume the development window) and record the run in §12.

### Majors

**M1. The block is four times shorter than the signal, and the power table ignores the block.**
The signal is a 252-day return sampled monthly (`rule_c3`, candidate_study.py:252-262): adjacent
monthly signals share 11/12 of their lookback and often the same side for a year. The registered
block is 3 months (:51, :388-394), and §7:217-225 admits only ">3 months" as uncaptured. But the
power numbers at §7:232-237 are `z·sd/√clusters` computed as if the blocks did not widen anything.
The true detection threshold is therefore ≥0.21 R and unknown before the run. A genuine 0.21 R C3
edge may return an interval that straddles zero, and §9:268-269 will record that as "no edge of
the detectable size" — a false negative written up as a finding. The same gap is the residual
false-pass channel: three USD-quoted pairs across a few multi-year dollar trends can supply most
of the mean while a 3-month-block interval stays narrow. The pre-registration's own docstring
(candidate_study.py:22-24) claims the blocks carry "one dislocation, one regime", which 3 months
cannot do for a 12-month signal.

**M2. The holdout's "inconclusive" category is not implemented, so a real edge will be recorded
as a failure.** The holdout has 34 monthly clusters and ~102 trades (2024-01 … 2026-10). At
sd ≈ 1.2 R a 90% lower bound detects ≈0.34 R, more once blocks are accounted for; §8:261-264 says
a smaller failure is "inconclusive". But `verdict` (candidate_study.py:537-565) has only
`pass`/`fail`/`no_decision`, and `no_decision` fires only below `n = 30` or `clusters = 30`
(:540-547). `n = 102`, `clusters = 34` clears both floors, so a straddling interval is recorded as
`status: "fail"`, `reason: "straddles zero"` (:558-561). The holdout is consumed and the machine
record says "did not survive" when the pre-registration says "too small to decide". Fix before the
run: add an `inconclusive` status keyed to a pre-registered minimum detectable effect, and have
the verdict document read it.

**M3. A C3 pass has no per-symbol gate.** The decision is on the pooled mean
(candidate_study.py:548); `by_symbol` is reported (:438-443) but never required. With three
USD-quoted pairs a single pair can carry the pooled mean while another is negative; the candidate
is then evidence for one pair's trend, not for "monthly time-series momentum on three majors".
At minimum this belongs in §9's verdict language; better, report the per-pair sign and require
agreement for a pass.

**M4. The three "independent" implementations are transcriptions, and they compare only what they
share.** `_tr_atr` (test:50-63) reproduces `atr` (candidate_study.py:110-125) branch for branch,
including the partial first window; `_fill` (test:75-96) reproduces `protective_exit`
(candidate_study.py:138-165) branch for branch; `_independent_c3` (test:194-224) reproduces the
month-start detection, the `< 252` guard and the `following <= last` end rule. They compare
entry/exit date, direction, reason and R (test:99-105, :405-433) — enough to catch an arithmetic
edit, not a shared misreading. The two choices that actually move C3's trade list (same-bar
re-entry, gap-before-intrabar ordering) are identical in both by construction. `_patch_tests7.py`
shows the independent C1 edited to match the engine's warm-up after the engine changed, i.e. the
opposite of an independent derivation. For C3 the only genuinely separate quantities are the
constant dicts (test:45-47), which are copied values.

**M5. The suite runs the development simulation, and a failure prints the trade list.**
`test_two_independent_implementations_of_c3` (test:425-433) compares two lists of
`(entry_date, exit_date, dir, reason, R)` with `==`; pytest's assertion rewriting prints both on
failure, as it would for the truncation comparison (test:488-489), and
`test_trades_obey_the_registered_window_and_arithmetic` (:452-463) asserts over real trades. §11:297-300's
"it prints no statistic, so it cannot leak a result" is wrong: a trade list is not a statistic but
it is the result. Combined with B3, the first run of the fixed suite is the highest-risk moment
for the "development window unseen" claim.

**M6. The evaluation never verifies the bars it scores.** `manifest_hash` hashes the manifest file
(candidate_study.py:92-93) — which is byte-identical to `docs/evidence/daily_cache_manifest.json`
and matches §3's `73d4359f…`, verified — but nothing in `build`/`main` re-hashes the five data
files; `fetch_daily_basket.py --check` (92-106) is a separate manual step. A silently edited
`EURUSD.json` (the C3 signal is 252 closes) changes the trade list while the evidence's
`cache_manifest_sha256` (:583-584) still matches the registered manifest, so the run looks clean.

### Minors

- **m1. 24 vs 30 clusters.** §7:230 says 30; §8:243 and :252 still say 24; `MIN_CLUSTERS = 30`
  (candidate_study.py:50). The patch trail shows why: `_patch_doc.py:4-5` set 12→24 at 15:36,
  `_patch_boot.py:5` set 30 at 15:37, `_patch_boot2.py:32` patched §7 but not §8. A candidate with
  25–29 clusters is pass-eligible under the registered rule and `no_decision` in code.
- **m2. `development_low` is always null** (candidate_study.py:507 reads `decision["low"]`;
  `verdict` at :537-565 never sets it). The lock records the digest but loses the development
  lower bound it was meant to preserve.
- **m3. §11:291 says "36 tests"; 37 are collected** (29 functions, of which three are
  parametrized 5/3/3: test:439, :451, :466).
- **m4. The warm-up is series-relative and the declared dev start is XAUUSD's first bar.**
  C3's effective first entry is 2005-02 (candidate_study.py:256); the lookback for early trades
  reads 2004-01-01…2004-06-10, which §4's table (prereg:90-94) does not list as any window. The
  "540-odd trades" estimate is ~537, so nothing statistical turns on it, but the boundary is
  undeclared.
- **m5. `simulate_monthly`'s `next_start is None` branch is unreachable in development and untested**
  (candidate_study.py:342-352). It is exactly the branch the holdout's final trade takes.
- **m6. The final trade pays a full round-trip transaction cost although it exits at the window
  close** (candidate_study.py:178 with :307, :352). Conservative, but §5:118-119 does not say it.
- **m7. §6:209 says the cost sweep is reported "with every result"; the holdout computes it only
  with `--cost-sweep`** (candidate_study.py:595).
- **m8. The audit's C3 row is loose.** §2:61 says `matrix2.json` ran EMA and Donchian "on exactly
  EURUSD, AUDUSD and NZDUSD"; `matrix2.json` has seven symbols (EURUSD, GBPUSD, AUDUSD, NZDUSD,
  US500, XAUUSD, XAGUSD), and it omits that `market_matrix.json` ran EMA/Donchian on EURUSD inside
  the holdout. The material claim — that the three C3 pairs were among those seen — is true.
- **m9. §2:34's "4 750 and 3 750 H1 cycles … roughly the last 9–10 months"** fits the first file
  (~9 months) but not the second (~7 months at 120 H1 bars/week).
- **m10. `--window holdout` writes the lock before scoring** (candidate_study.py:577-580), so a
  crash after the open burns the one permitted opening (recoverable only through B2's hole).
- **m11. The pre-run history rests on unversioned files.** §12:307-317 is reconstructed from
  mtimes and from `agent_data/_patch_*.py`, all under the gitignored `agent_data/`; nothing
  versioned records the 15:20–15:22 edits, including the C1 warm-up change at 15:22:05.
- **m12. The 0.83% tail is a raw percentile interval.** At ~179 clusters and 10,000 draws the
  Monte-Carlo standard error of the 0.84th percentile is ~0.004 R, and there is no BCa or
  studentisation; the seed makes a marginal pass reproducible, not robust (candidate_study.py:395-397).

### Statistics as they now stand

- **Multiplicity is right.** 98.33% two-sided leaves 0.83% per tail; three candidates give
  ≤2.5% family-wise (Bonferroni, no independence needed). The z = 2.394 and cluster units are
  correct. The two-stage gate is stronger still: a null that passes development *and* the 5%
  holdout is a ~0.04% event per candidate.
- **The interval is the binding constraint, not the double-cost gate.** For C3 the financing charge
  is already ≈0.15 R per monthly trade, so double cost subtracts ≈0.16 R of mean; a base mean of
  0.21 R still leaves +0.05 R at double cost, which passes. The gate is real but weak, and it is a
  point estimate with no lower bound.
- **A pass is hard to fake statistically and easy to fake procedurally.** The statistics are
  demanding: 100 trades, 30 clusters, a 0.83% lower bound, a one-shot 5% holdout. The procedure is
  not: the evidence document is trusted (B2), the lock is disposable (B2), and the verification
  suite has not run (B3).
- **A real edge of the quoted detectable size probably cannot pass.** 0.21 R is the i.i.d. best
  case; the block bootstrap can only widen it, and the double-cost gate and the 34-cluster holdout
  add further headroom (≈0.4–0.6 R to survive both stages). A true 0.21–0.3 R edge is therefore
  likely to be recorded as a null, and the prereg's §7/§9 language would overstate that null.

## What the design gets right

- **The audit is now narrow and mostly verifiable.** The manifest copy is byte-identical
  (`73d4359f…`), the probes are metadata-only (`agent_data/_probe_fx.py`, `_probe_d1.py`,
  `_probe_quality.py` print counts, dates and spreads, no returns), `matrix2.json` really records
  3 750 cycles, the folds file really is 8 × 1 175, and no other script in `docs/`, `scripts/` or
  `standalone/` mentions 2004 or 2019. The XAUUSD H1 sample starts 2023-06, so the pre-2020 daily
  claim survives every artefact the table lists.
- **The cache discipline is good:** write-once fetch, sha256 per file, `--check` that fails on a
  changed file, evaluation that never contacts the terminal, and decision dates taken from the
  broker's own `d` field.
- **The exit geometry is now right and pinned:** both gap opens before any intrabar level, the
  stop-before-target rule only inside the bar, and a dedicated test for the case that used to be
  booked as an impossible stop loss.
- **The time exit is specified to the bar** and pinned by prose, code and test at the same boundary.
- **Clustering is the correct unit for C3:** the three correlated pairs are resampled together
  inside a month, so the interval does not pretend the 537 trades are independent. That was the
  most important statistical fix and it is genuinely in place.
- **No buffer trade is possible.** I traced the month-start loop: entries before 2020-01-01 are
  skipped for the holdout and the loop breaks at the first month start after the dev end; only the
  2023-12 signal is read backwards, exactly as §4:93 says.
- **No look-ahead in C3:** the 252-day return uses closes at or before the signal bar; the
  protective scan stops at the day before the rebalance; the dev window's last trade is closed at
  the dev end; no 2020+ price can influence a dev trade.
- **Reporting is honest and useful:** reason histogram, per-symbol, per-direction, a real cost
  sweep at four multiples, a nullable profit factor, and a `status` that separates "underpowered"
  from "failed".
- **The disclosure list is unusually candid:** spread-floor provenance, direction-blind
  financing, XAGUSD signal-only, the C3 selection by data availability, the dollar factor, and the
  >3-month dependence the interval cannot capture.

## Residual risk if run as written

- **A C3 pass can be a dollar-trend episode.** The 3-month block is shorter than the 12-month
  signal, so a regime that spans 12–24 months is resampled as a handful of blocks; the interval is
  narrower than a regime-aware one, and the prereg says so but still passes on it. Any pass must
  be read against the per-year and per-pair breakdown, not just the interval.
- **The holdout is likely to fail a genuine edge and will call it a failure.** With 34 clusters,
  a dev-passing 0.2–0.3 R edge has a good chance of straddling zero, and `verdict` will write
  "fail" rather than "inconclusive" (M2). The one-shot holdout would then be spent for nothing.
- **The strongest holdout guarantee is social, not mechanical.** A forged two-key development
  document, a deleted gitignored lock, or a direct `build(..., "holdout")` call all defeat
  "exactly once" (B2). If the holdout is opened, the lock's null `development_low` and the absence
  of a versioned lock file leave a thin audit trail.
- **The first pytest run is a leak event, not a verification.** It executes the development
  simulation for all three candidates, and any parity failure prints the trade lists (M5). Run it
  with captured output, or move the parity tests onto synthetic data.
- **The `end` trade and its charges are slightly pessimistic:** marked at the last close but
  charged a full round trip, so C3's final month is understated by a few hundredths of R; harmless
  but undocumented.
- **The bars are never re-hashed at evaluation time** (M6), so the recorded provenance is only as
  good as the last manual `--check`.
