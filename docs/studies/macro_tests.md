# Four macro tests on the new data

Gold vs rates and the dollar, the VIX as a regime filter, the Deribit volatility premium,
and the yield curve. The purpose was to check whether the widely cited relationships are
predictive after costs, not merely correlated.

## 1. Gold against the 10-year yield and the dollar (2 510 daily observations)

| Relationship | Correlation |
|---|---|
| gold return vs change in the 10-year yield | **−0.30** |
| gold return vs dollar index return | **−0.44** |

Both links are real and have the documented sign. But they are **contemporaneous**: they
describe the same day, not the next one. A rule that longs gold for five days after a
20-day fall in yields produced:

```
497 trades, mean −0.104% per trade, 95% lower bound −0.269%
```

No edge. Knowing that gold and yields move opposite tells you nothing about which comes
first, and the test says the yield move does not lead.

## 2. VIX as a regime filter on SPY above its 200-day average

| Days | Mean daily return |
|---|---|
| all trend days | +0.041% |
| trend days with VIX < 20 | **+0.022%** |
| filtered mean, 95% interval | −0.010% .. +0.022% |

The filter **lowers** the return and its interval includes zero. The popular "only trade
when volatility is low" rule removes the best days along with the worst.

## 3. Deribit DVOL against 30-day realised BTC volatility (336 overlapping days)

Mean premium: **+1.5 volatility points**, i.e. implied is usually above realised — the
expected shape of a risk premium.

| Forward 5-day BTC return | Mean | n |
|---|---|---|
| when the premium is positive | +0.08% | 216 |
| when the premium is not positive | −0.42% | 115 |

The direction is suggestive (implied-rich periods are followed by flat-to-up returns,
implied-cheap by down), but with 336 days, one venue and no options execution, this is a
hypothesis, not a strategy. It also cannot be traded without selling volatility, which
needs an options account and margin.

## 4. Yield curve (10-year minus 13-week) and the next 20 days of SPY

| Curve state | Days | Mean forward 20-day return |
|---|---|---|
| inverted | 614 | **+1.35%** |
| normal | 1 878 | +1.05% |

Inversion — usually read as a recession warning — did not predict weaker 20-day equity
returns in this sample. A 20-day horizon is far too short for the recession mechanism,
which makes the honest reading: this table neither confirms nor refutes the classic claim,
and cannot be used as a timing rule.

## What the four tests establish

1. Macro correlations are real but synchronous; none of them leads price by a tradable
   interval in this data.
2. Regime filters built on volatility reduce returns here rather than improving them.
3. The one suggestive pattern — the crypto volatility premium — requires options data and
   execution that this repository does not have.
4. The curve's recession signal is invisible at a 20-day horizon.

## What would be needed to go further

- Options chains and a way to sell volatility (the premium test above).
- Monthly or quarterly horizons for the curve claim.
- A timestamped event database for anything news-driven.
