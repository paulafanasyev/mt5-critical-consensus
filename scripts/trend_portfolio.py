#!/usr/bin/env python3
"""Diversified trend following: the scheme that answers "how do people actually earn?".

Every rule tested so far was a single instrument with a tight stop, where one round trip
costs a third of the risk unit. Professional trend followers do something different:

  - many markets at once, because trends appear in a minority of them each year;
  - daily or weekly decisions, so the cost is a small fraction of the position;
  - volatility-scaled weights, so no single market can dominate the risk;
  - a slow 12-month signal, not an hourly one.

This script builds exactly that portfolio from the daily data we hold and reports the
annualised return, volatility, Sharpe ratio and worst drawdown, against a buy-and-hold
benchmark. Costs are charged on turnover.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKET = ROOT / "agent_data" / "market_data"
GOLD = ROOT / "agent_data" / "gold_data"
ASSETS = {
    "SPY": (MARKET, "spy"), "QQQ": (MARKET, "qqq"), "GLD": (MARKET, "gld"),
    "SLV": (MARKET, "slv"), "TLT": (MARKET, "tlt"), "EEM": (MARKET, "eem"),
    "GOLD_FUT": (MARKET, "gold_futures"), "OIL_FUT": (MARKET, "oil_futures"),
    "BTC": (MARKET, "btc_usd"), "ETH": (MARKET, "eth_usd"),
    "SHANGHAI": (MARKET, "shanghai"), "SHENZHEN": (MARKET, "shenzhen"),
    "CSI300": (MARKET, "csi300_etf"),
}
LOOKBACK = 252          # 12-month signal
VOL_WINDOW = 20         # realised volatility for weighting
REBALANCE = 21          # monthly
COST = 0.0005           # 5 bps per unit of turnover


def load(path: Path, name: str) -> dict[str, float]:
    payload = json.loads((path / f"{name}.json").read_text(encoding="utf-8"))
    bars = payload["bars"] if isinstance(payload, dict) else payload
    def stamp(bar):
        value = int(bar["t"])
        return value / 1000 if value > 10**12 else value
    return {datetime.fromtimestamp(stamp(bar), tz=timezone.utc).date().isoformat():
            float(bar["c"]) for bar in bars}


def main() -> int:
    series = {label: load(path, name) for label, (path, name) in ASSETS.items()}
    days = sorted(set.intersection(*(set(value) for value in series.values())))
    print(f"assets {len(series)}, common days {len(days)}  {days[0]} .. {days[-1]}")

    holdings: dict[str, float] = {}
    portfolio_returns = []
    benchmark_returns = []
    last_rebalance = -1

    for index in range(LOOKBACK + VOL_WINDOW, len(days) - 1):
        today, tomorrow = days[index], days[index + 1]

        if index - last_rebalance >= REBALANCE:
            last_rebalance = index
            new_holdings: dict[str, float] = {}
            wanted = {}
            for label, prices in series.items():
                base = days[index - LOOKBACK]
                signal = prices[today] / prices[base] - 1
                window = [prices[days[i]] / prices[days[i - 1]] - 1
                          for i in range(index - VOL_WINDOW, index)]
                vol = statistics.pstdev(window) * math.sqrt(252) if len(window) > 5 else 0
                if vol <= 0 or signal == 0:
                    continue
                weight = min(1.0, 0.20 / vol) * (1 if signal > 0 else -1)
                wanted[label] = weight
            gross = sum(abs(value) for value in wanted.values()) or 1.0
            new_holdings = {label: value / gross for label, value in wanted.items()}
            turnover = sum(abs(new_holdings.get(label, 0) - holdings.get(label, 0))
                           for label in set(new_holdings) | set(holdings))
            holdings = new_holdings
            portfolio_returns.append(-turnover * COST)
        else:
            portfolio_returns.append(0.0)

        daily = 0.0
        for label, weight in holdings.items():
            prices = series[label]
            if today in prices and tomorrow in prices and prices[today]:
                daily += weight * (prices[tomorrow] / prices[today] - 1)
        portfolio_returns[-1] += daily
        benchmark_returns.append(
            statistics.fmean([series[label][tomorrow] / series[label][today] - 1
                              for label in series if today in series[label]
                              and tomorrow in series[label]
                              and series[label][today]]))

    def summarise(label: str, returns: list[float]) -> None:
        n = len(returns)
        total = math.prod(1 + value for value in returns) - 1
        annual = (1 + total) ** (252 / n) - 1 if n else 0
        vol = statistics.pstdev(returns) * math.sqrt(252)
        equity, peak, drawdown = 1.0, 1.0, 0.0
        for value in returns:
            equity *= 1 + value
            peak = max(peak, equity)
            drawdown = max(drawdown, 1 - equity / peak)
        print(f"{label:24} total {total * 100:+8.1f}%   annual {annual * 100:+6.1f}%   "
              f"vol {vol * 100:5.1f}%   Sharpe {annual / vol if vol else 0:5.2f}   "
              f"maxDD {drawdown * 100:5.1f}%")

    summarise("trend portfolio (long/short)", portfolio_returns)
    summarise("benchmark equal-weight", benchmark_returns)
    if sum(1 for value in portfolio_returns if value > 0) / len(portfolio_returns) > 0.5:
        print("positive days:", round(sum(1 for value in portfolio_returns if value > 0)
                                      / len(portfolio_returns), 3))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
