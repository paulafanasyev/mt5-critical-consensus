# Demo run results on real data

Russian is the base version. English: this file · Tiếng Việt: [DEMO_RESULTS.vi.md](DEMO_RESULTS.vi.md).

The run was made on 2026-11-01 on the host with MetaTrader 5 installed (build 6296), demo account
`114929502` on `MetaQuotes-Demo`, account currency USD, starting equity 100,000. The bars are the
broker's own real H1 bars, downloaded from the terminal through `copy_rates_range`; no step of this
run contained a trading call, and every journal record carries `broker_order_sent: false`.

## What was executed

```powershell
PYTHONPATH=src python scripts/run_mt5_agent.py run `
  --policy agent_data/demo_eurusd_real.yaml --source live --live-bars 600 `
  --balance 100000 --currency USD --db agent_data/demo_eurusd.db --language en --execution auto --verbose
PYTHONPATH=src python scripts/run_mt5_agent.py run `
  --policy agent_data/demo_xauusd_real.yaml --source live --live-bars 600 `
  --balance 100000 --currency USD --db agent_data/demo_xauusd.db --language en --execution auto --verbose
```

The policies live in `agent_data/` (not part of the repository): `risk_per_trade_fraction: 0.0002`,
`min_risk_reward: 1.5`, `max_open_positions: 1`, `execution_mode: auto`, `require_demo: true`, plus
pinned `expected_login` / `expected_server`. Instrument specs were read from the terminal: EURUSD —
point 0.00001, tick value 1.0, volume 0.01/0.01/500; XAUUSD — point 0.01, tick value 0.1, volume
0.01/0.01/100.

## Run outcome

| Instrument | Cycles | Closed trades | Wins | Win rate | Realized P/L | Equity | Min / max equity | Journal chain |
|---|---|---|---|---|---|---|---|---|
| XAUUSD | 540 | 23 | 5 | 0.217 | −159.96 | 100,000 → 99,840.04 | 99,756.24 / 100,018.21 | `valid: true`, 1104 events |
| EURUSD | 540 | 24 | 4 | 0.167 | −203.39 | 100,000 → 99,796.61 | 99,791.41 / 100,044.31 | `valid: true` |

No cycle sent an order to the broker; 501 (XAUUSD) and 475 (EURUSD) cycles were blocked by the
single-open-position cap, 11 and 19 by the absence of a signal, 5 and 22 by the trading window.

![Equity curves](charts/demo_equity_curves.svg)

![R multiple per closed trade](charts/demo_trade_r_multiples.svg)

## Why the result looks like this

The stop is 2 × ATR and the target is 2R. For a driftless walk the probability of reaching +2R
before −1R is exactly 1/3, and the expectancy before costs is zero. The observed win rates (0.217
and 0.167) are **below** that threshold, which is why a per-trade loss appeared even at a tiny risk:
−6.95 USD per trade on gold and −8.47 USD on EURUSD.

A separate measurement over a year of the same broker bars confirms it: the lag-1 autocorrelation of
hourly returns is negative (XAUUSD −0.019, EURUSD −0.0067), so after a run of rising bars the next
bar tends to retrace, while SMA(5/20) enters exactly when the fast average is already above the slow
one. With 23 and 24 trades these results do not statistically prove the strategy loses
(t = −1.57 and −1.95), but they leave no basis for calling it profitable.

## Edge research on real bars

`scripts/render_demo_evidence.py` recomputes both strategy families on the same genuine bars, never
fitting parameters to the tested window, and repeats the sweep at several costs per side:

![Cost dependence](charts/real_bar_cost_sweep.svg)

| Instrument | Cost per side | Bollinger(20, 2.0) | SMA(5/20) |
|---|---|---|---|
| XAUUSD | 0 bps | −33.5% (219 trades) | +54.9% (344) |
| XAUUSD | 5 bps | −46.6% | +9.9% |
| XAUUSD | 20 bps | −72.4% | −60.9% |
| EURUSD | 0 bps | +2.1% (275 trades) | −0.3% (382) |
| EURUSD | 5 bps | −22.4% | −32.0% |
| EURUSD | 20 bps | −66.1% | −78.4% |

Zero cost is a physically impossible case, not an upper bound: it shows that neither family has any
margin of safety. The one measured signal is the hour-of-day profile:

![Hourly t-statistics](charts/real_bar_hourly_tstats.svg)

Gold gives t = +3.25 at 09 UTC and +2.51 at 03 UTC over 5742 observations; on EURUSD the largest
positive hour is +1.65 while the most extreme hour is negative, t = −2.00 at 04 UTC.
This is a hypothesis, not an edge: after correcting for 24 comparisons only borderline significance
survives on the gold hour, and it has to be tested on a separate period.

## Cross-check against the parallel workspace

`C:\Users\Administrator\.zcode\workspace\default` contains independent work on the same idea using
public Binance candles (not MT5): a 12-fold walk-forward, 32 gold strategy configurations, and
"live" virtual runs. Its conclusions match ours: trend families are negative, in-sample winners
collapse out-of-sample (PF 1.4 → 0.48), gold is mean-reverting (their lag-1 −0.015 against our
−0.019 on broker bars), and the single family that survived there — Bollinger(20, 2.0) on EURUSDT at
+7.1% — **does not reproduce** on the broker's real bars (−22.4% already at 5 bps per side). Their
"live" outcome balances: 1000 → 1004, 1000 → 996, 996.70, 998.33, +0.08, −0.57.

## What this run proves and what it does not

Proven: the chain "real broker bars → policy gates → sizing → paper execution → hash-linked journal"
works and is reproducible with the command above; the per-trade risk held (every stop −18…−20 USD
against a 20 USD budget); no order reached a broker.

Not proven: live execution at the broker. At the time of the run the market was closed for the
weekend (the last EURUSD tick was 20.4 hours past the allowed age, XAGUSD had no tick, US500 reported
`trade_mode = 0`), so the path `scripts/run_mt5_demo_trading.py` + `scripts/mt5_demo_bridge.py` + the
EA is blocked by the `QUOTE_STALE` gate — that is expected behaviour, not an execution result. The EA
is also not attached to a chart and `Config/common.ini` carries no allowlist yet, so broker-side
execution confirmation remains PENDING EXTERNAL.
