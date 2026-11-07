# Data inventory after the download round

What is now on disk, what it enables, and what is still missing. All files are JSON under
`agent_data/market_data/` (gitignored) and were fetched with `scripts/fetch_market_data.py`.

## Downloaded (daily, ten years, Yahoo Finance chart API)

| Name | Series | Bars | From |
|---|---|---|---|
| `vix` | CBOE volatility index | 2 516 | 2016-10-31 |
| `yield_10y` | US 10-year yield (^TNX) | 2 513 | 2016-10-31 |
| `yield_13w` | US 13-week bill (^IRX) | 2 513 | 2016-10-31 |
| `dollar_index` | DXY | 2 514 | 2016-11-01 |
| `spy`, `qqq` | US equity indices via ETFs | 2 514 | 2016-10-31 |
| `gld`, `slv` | gold and silver ETFs | 2 514 | 2016-10-31 |
| `tlt` | long Treasuries | 2 514 | 2016-10-31 |
| `eem` | emerging markets | 2 514 | 2016-10-31 |
| `gold_futures` | front-month gold futures | 2 514 | 2016-11-01 |
| `oil_futures` | front-month WTI futures | 2 514 | 2016-11-01 |
| `btc_usd`, `eth_usd` | spot crypto | 3 653 / 3 280 | 2016-11 / 2017-11 |
| `btc_dvol`, `eth_dvol` | Deribit volatility index | 366 each | one year |

FRED and Stooq were tried first: FRED timed out from this host and Stooq returned an HTML
block page, so Yahoo was used instead. Both are worth retrying from another network.

## What this now makes testable

1. **Cross-sectional momentum** — done: see below.
2. **Gold vs real rates** — nominal 10-year yield instead of TIPS (real yield is not
   available without FRED); a proxy, stated as such.
3. **Dollar and gold** — DXY against gold, a classic negative link that can be measured.
4. **Volatility regime filter** — VIX level or change as a gate on equity or gold rules,
   and DVOL for crypto.
5. **Curve slope** — 10-year minus 13-week as a recession/carry proxy.

## What is still missing

- **Options chains** (per-strike implied vol) — needed for a true volatility-risk-premium
  study; DVOL only gives the index level.
- **Futures term structure** (several expiries) — needed for a real carry study; only the
  front month is available.
- **Real yields** (TIPS) — nominal yield is a proxy with inflation noise.
- **Intraday history** beyond what the broker terminal serves (hourly only, back to 2023).
- **A timestamped news database** — needed for any honest event study.

## First result on the new data

Cross-sectional momentum, 12-1, long the top half and short the bottom half, monthly
rebalance, 0.1% per side, 2017-11..2026-10, seven assets:

```
106 rebalances, wins 52.8%, mean -0.71% per month,
95% lower bound -1.93%, t = -0.98
verdict: no edge after costs
```

The documented effect does not appear on this small, highly correlated universe: seven
assets that mostly share one equity/risk factor are not a cross-section, and the cost of
rebalancing both legs every month is larger than the spread it might capture.

## Honest summary

The data gap that mattered most is now partly closed. The first family it unlocked still
failed, which is consistent with everything else in this programme — but the tests that
the new data enables (rates vs gold, vol regime, dollar) are worth running because they
are different in kind from the directional rules that failed.
