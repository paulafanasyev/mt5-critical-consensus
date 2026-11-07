# Chinese strategies, tested on twelve years of index data

The strategies Chinese retail and quant communities talk about most, checked against free
daily data (Shanghai Composite, Shenzhen Component, CSI 300 ETF via 510300.SS), with costs
charged on every position change. Nothing here is a promise; two of the four are not even
objectively testable.

## The four that can be tested

### 1. 双均线 — dual moving average

| Market | 5/20 | 20/60 |
|---|---|---|
| Shanghai Composite | −0.4%/yr, 102 trades, 33% wins, DD 57% | −2.3%/yr, 27 trades, 30% wins |
| Shenzhen Component | **+2.1%/yr**, 92 trades, 29% wins, DD 54% | −2.6%/yr, 25 trades |
| CSI 300 ETF | −0.8%/yr, 99 trades, 30% wins, DD 47% | −1.7%/yr, 31 trades |

No stable edge: the fast pair is noise around zero, the slow pair loses everywhere, and
every curve carries a 50%+ drawdown.

### 2. 隔夜动量 — overnight momentum, and its mirror

This is the one real finding. Holding the index **overnight** loses money systematically:

| Market | Mean overnight | t-stat | Short-overnight, net at 0.02% cost | at 0.05% | at 0.10% |
|---|---|---|---|---|---|
| Shanghai Composite | **−0.085%/day** | **−7.11** | **+16.3%/yr** | +8.7%/yr | −3.9%/yr |
| Shenzhen Component | −0.050%/day | −3.48 | +7.6%/yr | 0.0%/yr | −12.6%/yr |
| CSI 300 ETF | −0.048%/day | −3.95 | +7.0%/yr | −0.5%/yr | −13.1%/yr |

The effect is statistically strong (t up to −7), but the sign of the *tradable* result
depends entirely on execution cost: it pays only with futures-level costs (IF/IC contracts,
roughly 0.01–0.02% per round trip) and disappears for stock or ETF accounts.

### 3. 短期反转 — short-term reversal

Buying after a 5-day fall and holding 5 days: mean +0.04% per trade, **t ≈ 0.5** on all three
markets. The documented A-share reversal is not visible at this scale and cost.

### 4. 网格 — grid trading

The naive one-unit grid produced 2–136 fills and returns from +0.1% to +24% depending on
the market, but the implementation is too crude to trust (it assumes infinite liquidity at
the grid level and no gap handling). Recorded as **not tested properly**, not as a result.

## The two that cannot be tested here

- **打板 / chasing limit-up stocks** needs intraday price and volume for individual names,
  plus the 10% daily limit rule. Index data cannot answer it.
- **缠论 (Chan theory)** is a discretionary framework of pivots and levels with no single
  mechanical definition; a backtest would test the implementation, not the theory.

## What this establishes

1. The famous dual-MA system has no edge on Chinese indices after costs.
2. The strongest measurable Chinese anomaly is the **negative overnight move**, and its
   mirror trade is viable only with very cheap execution.
3. Reversal, at this scale, is invisible.
4. The two most talked-about approaches (limit-up chasing, Chan theory) are outside what
   this data can honestly evaluate.

## What would be needed to go further

- A Chinese futures account or data for IF/IC contracts, to test the overnight trade with
  realistic costs and margin.
- Tick-level data for a sample of A-share names, to test limit-up behaviour honestly.
- A written, mechanical definition of the Chan rules before any test — otherwise the
  backtest measures the coder, not the theory.
