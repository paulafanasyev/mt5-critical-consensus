# From the demo lab to the real account

The operator's plan, written down as a protocol: **experiment freely on the demo, and move to
the real account only what has proven itself there.** Losses on the demo are the cost of
research and are accepted; the goal is income on the main account.

## Stage 0 — the lab (now)

- Account: `114929502` / `MetaQuotes-Demo`, equity ≈ 99 091, mode: paper.
- Thirty agents, eight schemes, unlimited losses accepted here.
- Purpose: generate trades and evidence, not income.
- Rules of the lab: everything is logged; nothing is judged before 100 closed trades.

## Stage 1 — candidate

A scheme becomes a candidate when **all** of the following hold on demo trades:

| Gate | Threshold |
|---|---|
| closed trades | ≥ 100 |
| net expectancy | > 0 after real spread and slippage |
| confidence | 95% bootstrap lower bound > 0 |
| drawdown | ≤ 15% of the capital allocated to that scheme |
| documentation | written rule, no parameter changes after the fact |
| cost model | the broker's real spread, not an assumption |

A scheme that fails any gate stays in the lab. A scheme that passes is still not promoted —
it goes to Stage 2.

## Stage 2 — shadow on the real venue

The scheme runs on the real account **without sending orders** for at least two weeks:

- signals are recorded at the moment they appear;
- hypothetical entry, stop, target and exit are logged;
- the hypothetical results are compared with the demo results on the same period;
- the gap between demo and shadow is the honest estimate of venue differences
  (spread, slippage, execution delay, swap).

If the shadow turns negative while the demo is positive, the scheme is rejected: the demo
was flattered by its fill assumptions.

## Stage 3 — real micro

Only after Stage 2 passes:

| Parameter | Value |
|---|---|
| risk per trade | **0.10–0.25%** of the real balance |
| hard daily stop | −0.5% of balance, then the account is flat until the next day |
| maximum drawdown | −3% of balance, then the scheme is switched off |
| position cap | one position per symbol, no averaging down |
| exits | breakeven at +0.5R, give back no more than a third of the peak, staged scale-in on profit |

The first real trades are an execution test: 20 trades are enough to measure real slippage,
and the scheme must stay positive after it.

## Stage 4 — scale

Scaling is earned, not chosen:

- 100 real trades with positive net R and the same gates as Stage 1 → risk may double;
- any breach of the daily or drawdown stop → size halves and the scheme returns to shadow;
- never more than **0.5% risk per trade** and **2% per day** on the real account.

## Technical setup for the real account

1. **Separate terminal instance.** A portable copy of MetaTrader 5 with its own data folder,
   logged into the real account. The demo terminal and the lab swarm stay untouched.
2. **Separate runner.** The real runner uses its own policy with `require_demo: false`, its
   own database and its own magic range, so the two books can never mix.
3. **The existing demo guard stays.** The current EA refuses a live account by design; real
   execution must go through a separately reviewed path, not by flipping that check.
4. **No password in chat.** The operator logs the real account into the second terminal; the
   Python API then works through that logged-in session. I never need the password.

## What is already built and will carry over

- Staged scale-in on profit (+15/+30/+50/+75, half the position each time).
- Breakeven at a profit peak, giveback exit at a third of the peak.
- Dollar stop scaled to the position's own risk, not a fixed ten dollars.
- Churn guard: at most three entries per symbol per hour, thirty-minute cooldown after a stop.
- Per-symbol caps, a −1% account brake, a supervisor every minute, a keepalive every five.

## What the operator must decide

1. Which broker and which account will be the real one; open its **demo** first, so the same
   venue can be tested before any money moves.
2. Whether the first real size is 0.10% or 0.25% per trade; the protocol allows both.
3. That a scheme failing Stage 1 is dropped, not re-tuned until it passes.
