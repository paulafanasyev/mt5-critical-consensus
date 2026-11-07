# Round 2 design reviews — disposition

Three fresh critics, one per candidate, reviewed the revised pre-registration, the engine and
the test suite on 2026-11-01. Their full reviews are committed beside this file
(`c1_inside_breakout_round2.md`, `c2_ratio_reversion_round2.md`, `c3_fx_tsmom_round2.md`).
All three still returned FIX_FIRST. This file records what was changed in response and what is
accepted as residual risk.

## What round 2 confirmed as fixed

- The time exit is exact and pinned by a test that asserts the exit **date**.
- The gap ordering is correct: both opens are resolved before any intrabar level, so a
  favourable gap can no longer be rebooked as a stop.
- The re-entry rule is stated and implemented consistently.
- The bootstrap is a real moving-block estimator over entry months, not a singleton no-op.
- The three "independent" implementations are now written from the document with their own
  ATR, constants and cost arithmetic, and compare direction, exit reason and R.
- The joint-date alignment of C2 and the month boundaries of C3 are correct, and the 252/253
  off-by-one is gone.

## Blockers raised in round 2 and what was done

| Finding | Disposition |
|---|---|
| Cluster floor contradicted itself: 30 in §7, 24 in §8, 30 in code | **Fixed.** 30 everywhere, including the holdout. |
| The double-cost gate was skipped on the holdout and optional in `verdict()` | **Fixed.** The sweep is always priced for both windows and `verdict()` requires `double_cost_mean`; there is no `None` path. |
| The holdout gate could be opened by a fabricated `passed: true` document; the lock lived in gitignored `agent_data/`; no engine hash | **Fixed.** `open_holdout` re-runs development and refuses unless the recorded n, clusters, mean and lower bound match; the lock now lives in the tracked `docs/evidence/holdout_locks.json` and carries the sha256 of the evidence, of this engine and of the data manifest; `require_holdout_open` refuses if any of the three moved. This is tamper-**evident**, not tamper-proof, and §8 says so: a determined operator can still edit code and delete the lock, but the diff and the deletion are in git. |
| §2 still overclaimed, and §12's timeline was incomplete | **Fixed.** §2 now claims only that no statistic for a pre-2020 daily path existed, names the suite execution, and §12 records the post-fix test runs. |
| `_fill`, the test helper, was a transcript of `protective_exit` | **Fixed.** Rewritten with signed comparisons (`(price − level) × side ≤ 0`) rather than the engine's explicit branches. |
| The warm-up test was vacuous | **Fixed.** The fixture now contains a shape that *would* signal at index 29 and asserts it does not, with the preconditions asserted. |
| No test pinned the bootstrap's width — a `[mean, mean]` implementation would pass | **Fixed.** A dispersion test and a block-mechanism test (`block == number of months` must collapse to the point estimate). |
| C3 exits at the open of month M+1 and enters at that same open, against the common re-entry clause | **Documented, not changed.** §5 C3 states the exception explicitly and why it is not look-ahead: the signal is formed on the last close of month M, before that open, and each side pays its own round trip. |
| The block length (3 months) is short against a 12-month signal, so §7's power table is an i.i.d. best case | **Documented.** §7 now says the block bootstrap widens the interval relative to an i.i.d. month bootstrap, so the quoted detectable edge is a lower bound on what the design can see. |
| C2 is a difference-of-returns z-score, not a lead-lag regression, and can be carried by gold drift | **Documented.** Renamed to ratio reversion, and a drift confound paragraph added beside C1's. |

## Accepted residual risk, stated rather than fixed

- **Local code is not a security boundary.** Anyone with write access can delete the lock and
  re-run. The claim is tamper-evidence in git, not prevention.
- **Interval width is a lower bound on the true uncertainty.** A block of three months does not
  capture a multi-year regime, and no bootstrap over 15 years of one path can.
- **Holdout power is weak.** 34 monthly clusters detect ≈ 0.34 R at 90%; a smaller real edge
  that passes development will most likely produce a straddling holdout interval, which the
  verdict will call **inconclusive**, not a refutation.
- **The cost model is era-invariant** and its financing term is direction-blind. §6 states the
  direction of the bias for metals and for AUD/NZD carry rather than correcting it.
- **The development window was executed by the test suite before the stamp**, with no statistic
  printed. The blind is intact in the only sense that can be verified here: no mean R, count or
  interval for the development window had been produced or inspected.
