# The crypto volatility premium: real, decaying, and dangerous

Deribit publishes DVOL, the 30-day implied volatility of BTC/ETH options. The premium is
measured directly: implied minus the volatility that actually followed over the next 30
days, and the settlement P/L of a variance swap sold at DVOL and settled 30 days later.
Data: 2 049 daily observations per asset, 2021-03-24 .. 2026-11-01.

## Full sample

| | mean premium (vol points) | short-vol P/L (variance points) | win rate | after 1.5-pt spread | worst settlements |
|---|---|---|---|---|---|
| **BTC** | **+8.78** [CI +8.24, +9.31] | **+10.44** [CI +9.49, +11.40] | 72.9% | +8.67 [+7.72, +9.62] | −69.6, −67.4, −62.6 |
| **ETH** | **+5.26** [CI +4.53, +5.97] | **+6.55** [CI +4.69, +8.39] | 66.6% | +4.35 [+2.50, +6.20] | −295.0, −274.6, −273.9 |

This is the first relationship in the whole programme that survives a cost haircut over the
full sample: selling volatility paid, and the interval is far from zero.

## But it is decaying

Short-vol P/L by year (variance points):

| Year | BTC | ETH |
|---|---|---|
| 2021 | +30.9 | +21.3 |
| 2022 | +16.5 | +16.5 |
| 2023 | +6.3 | +6.0 |
| 2024 | +6.9 | +3.0 |
| 2025 | +5.1 | −5.4 |
| 2026 (to Nov) | **−1.3** | **−0.4** |

The premium has been competed away: the last two years are zero or negative. A strategy
that sold volatility in 2021 would have looked brilliant and by 2026 would be paying for
the privilege.

## And it is a tail-risk business

The worst settlements are −70 variance points for BTC and **−295 for ETH**: a single crash
month undoes years of premium. This is not a rule that "always wins"; it is a business of
collecting small amounts and occasionally losing a large one, which requires collateral,
margin management and the ability to survive the tail.

## What this establishes

1. The volatility risk premium is real in crypto options, at least over 2021–2024.
2. It has shrunk to roughly zero in 2025–2026 — consistent with an arbitraged market.
3. It cannot be captured by any directional spot rule; it needs an options account and
   short-vol execution.
4. It is a different *business*, not a faster rule: margin, collateral, tail risk and
   option execution costs all matter.

## What would be needed to test it as a business

- An options account on Deribit (or a venue that allows short vol with margin).
- Real option quotes (bid/ask per strike), not just the index.
- A collateral and liquidation model, plus a tail-risk budget.
- A pre-registered entry rule (e.g. sell 30-day variance when the premium exceeds 5 vol
  points), with a stated maximum notional and a defined stop for the tail.

None of that exists in this repository and none of it is demo-testable without the account.

## Honest verdict

Real premium, decaying, tail-heavy. Worth paper-testing if an options account appears;
not a reason to touch real money today, and not a directional strategy that can be bolted
onto the spot demo.
