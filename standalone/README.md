# Standalone MT5 expert + research portfolio

This folder imports the second delivery line of the project — the work that lived
outside the git repository (in the `.zcode` workspace) until 2026-11-01. It is kept
here so the codebase is complete in one branch.

**It is not connected to the Python gates in this repository.** The expert is a
self-contained MQL5 program: five agents (EMA trend, RSI momentum, ATR volatility,
price action, session) vote inside the terminal, the certainty gate is a strict
conjunction, and there is a Telegram approval card. It never calls the localhost
bridge, the FastAPI policy endpoints or `evaluate_signal_bundle`.

## What is inside

| Path | What it is |
|---|---|
| `mql5/CriticalConsensusStandalone.mq5` | the zero-install expert source (compiled with the real MQL5 compiler: 0 errors, 0 warnings) |
| `mql5/CriticalConsensusStandalone.ex5` | the compiled artifact users drop into `MQL5/Experts/` |
| `mt5-ready/` | two-minute install cards in Russian and Vietnamese + the same source/artifact |
| `docs/install_ru.md`, `docs/install_vi.md` | full install guides (MT5-only path and the Python path) |
| `docs/strategy_playbook.md` | knowledge base of 12 strategy families, their limits and the scam checklist |
| `scripts/live_portfolio.py` | multi-strategy paper portfolio on real Binance quotes (4 slots, ATR stops, circuit breaker) |
| `scripts/live_trading.py`, `scripts/demo_real_data.py` | single-symbol live demo on public quotes |
| `scripts/capital_tiers.py`, `scripts/backtest*.py`, `scripts/gold_research.py`, `scripts/research_walkforward.py` | capital tiers and the research sweeps whose results are quoted in the root `CHANGELOG.md` |
| `scripts/run.py`, `start.bat`, `start_autonomous.bat` | the double-click launcher for the Python demo |
| `reports/` | the JSON/text evidence produced by those sweeps |

## Status — read before trusting anything here

- **Demo only.** The expert refuses to trade on a non-demo account
  (`ACCOUNT_TRADE_MODE != DEMO` → refusal), caps autonomous volume at 0.05 lots and
  three trades a day.
- **No verified edge.** `backtest_multi.py` found no edge for the SMA family after
  costs; `research_walkforward.py` reports +7.1% chained out-of-sample for Bollinger
  mean-reversion on EUR and +2.4% for Donchian; `gold_research.py` reports +84.5%
  chained OOS for EMA 50/200 on gold (7/8 folds positive). None of it accounts for
  swaps, real spreads, slippage or broker execution, and none of it is a promise.
- **Known defects in the portfolio runner** (recorded 2026-11-01, unfixed here):
  `fetch_candles` always requests 1-minute bars although slots declare 1h, and
  `signal_ematrend` hardcodes EMA 12/26 instead of using the slot's 50/200 — so the
  paper portfolio does not run the rules that were researched. Treat its numbers as
  invalid until fixed.
- `live_portfolio.py` writes `portfolio_log.json` next to itself; logs are not
  committed here.
