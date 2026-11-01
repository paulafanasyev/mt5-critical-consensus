# Cross-asset relationships, 2020-08 .. 2026-11

Daily returns of PAXG (gold), BTC, ETH and EUR/USD from Binance, aligned on 2 256 common
days. The question is not "is there a story" but "is there a stable relationship a rule
could use".

## Full-sample correlation of daily returns

| | gold | BTC | ETH | EUR/USD |
|---|---|---|---|---|
| **gold** | 1.00 | **0.15** | 0.14 | **0.31** |
| **BTC** | 0.15 | 1.00 | **0.81** | 0.27 |
| **ETH** | 0.14 | 0.81 | 1.00 | 0.26 |
| **EUR/USD** | 0.31 | 0.27 | 0.26 | 1.00 |

## The correlations are not stable

Rolling 90-day correlation with gold:

| Pair | min | median | max |
|---|---|---|---|
| gold vs BTC | −0.40 | +0.18 | +0.63 |
| gold vs ETH | −0.40 | +0.14 | +0.60 |
| gold vs EUR/USD | −0.30 | +0.35 | +0.74 |

A pair that swings from −0.40 to +0.63 inside six years cannot carry a fixed hedge ratio:
any rule calibrated on one window is wrong in the next.

## No lead-lag signal

Correlation of gold's return today with BTC's return `k` days earlier:

| lag | −5 | −4 | −3 | −2 | −1 | **0** | +1 | +2 | +3 | +4 | +5 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| corr | −0.01 | +0.02 | −0.02 | 0.00 | −0.02 | **+0.15** | −0.01 | −0.01 | −0.03 | −0.01 | +0.04 |

Only the contemporaneous correlation is non-zero; every lag is indistinguishable from
noise. There is no "BTC moves first, gold follows" at daily resolution — the one thing a
cross-asset timer would need.

## What this does and does not give

- **Does:** confirms that gold and crypto are weakly related (0.15) and therefore useful
  as *diversifiers* in a portfolio, and that BTC and ETH are nearly the same bet (0.81).
- **Does not:** give a tradable rule. The relationship is contemporaneous, unstable, and
  fully visible only after both legs have moved — exactly the information a timer needs
  *before* the move.

## What would be needed to go further

- Intraday (minute) data for both legs, to test whether the contemporaneous link is
  exploitable with a few seconds of lead.
- A timestamped news database, to test whether an event moves one leg before the other.
- Neither exists in this repository, and both would have to be pre-registered before use.

## Bottom line

No cross-asset edge is visible: correlations are weak and unstable, lead-lag is zero. The
honest use of this table is portfolio construction (diversification), not prediction.
