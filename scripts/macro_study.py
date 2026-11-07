#!/usr/bin/env python3
"""Four macro tests on the newly downloaded data.

1. Gold against the 10-year yield and the dollar index.
2. The VIX as a regime filter on an equity trend rule.
3. The Deribit volatility index against realised crypto volatility.
4. The yield curve (10-year minus 13-week) and forward equity returns.

Each section reports numbers with a bootstrap interval where a rule is involved; none of
them is a promise. The point is to see which of these widely cited relationships survives
contact with costs and out-of-sample arithmetic.
"""
from __future__ import annotations

import json
import math
import random
import statistics
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "agent_data" / "market_data"
BOOTSTRAP = 10_000


def load(name: str) -> dict[str, float]:
    payload = json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))
    return {datetime.fromtimestamp(bar["t"] / 1000, tz=timezone.utc).date().isoformat(): bar["c"]
            for bar in payload["bars"]}


def returns(series: dict[str, float]) -> dict[str, float]:
    days = sorted(series)
    return {days[i]: series[days[i]] / series[days[i - 1]] - 1.0 for i in range(1, len(days))}


def corr(a: list[float], b: list[float]) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a) / n)
    sb = math.sqrt(sum((y - mb) ** 2 for y in b) / n)
    return cov / (sa * sb) if sa and sb else 0.0


def bootstrap_lower(values: list[float], confidence: float = 0.95) -> tuple[float, float]:
    generator = random.Random(20261101)
    n = len(values)
    means = sorted(sum(values[generator.randrange(n)] for _ in range(n)) / n
                   for _ in range(BOOTSTRAP))
    return means[int((1 - confidence) * (BOOTSTRAP - 1))], statistics.fmean(values)


def section_gold_yield_dollar() -> None:
    gold = returns(load("gld"))
    dollar = returns(load("dollar_index"))
    yields = load("yield_10y")
    days = sorted(set(gold) & set(dollar) & set(yields))
    dyield = {days[i]: yields[days[i]] - yields[days[i - 1]] for i in range(1, len(days))}
    common = days[1:]
    print("\n1. gold vs 10-year yield and the dollar (daily)")
    print(f"   days {len(common)}")
    print(f"   corr(gold, delta yield)  {corr([gold[d] for d in common], [dyield[d] for d in common]):+.3f}")
    print(f"   corr(gold, dollar)       {corr([gold[d] for d in common], [dollar[d] for d in common]):+.3f}")

    trades = []
    step = 20
    for index in range(step, len(common) - 5, 5):
        change = yields[common[index]] - yields[common[index - step]]
        direction = 1 if change < 0 else -1  # yield falling -> long gold
        period = [gold[d] for d in common[index:index + 5]]
        gross = direction * (math.prod(1 + value for value in period) - 1)
        trades.append(gross - 0.001)
    low, mean = bootstrap_lower(trades)
    print(f"   rule: 5-day hold when yields fell over 20 days, {len(trades)} trades, "
          f"mean {mean * 100:+.3f}%, 95% lower {low * 100:+.3f}%")


def section_vix_filter() -> None:
    spy = load("spy")
    vix = load("vix")
    days = sorted(set(spy) & set(vix))
    prices = [spy[d] for d in days]
    trend_returns, filtered_returns = [], []
    for index in range(200, len(days) - 1):
        if prices[index] <= statistics.fmean(prices[index - 200:index]):
            continue
        day_return = prices[index + 1] / prices[index] - 1
        trend_returns.append(day_return)
        if vix[days[index]] < 20:
            filtered_returns.append(day_return)
    print("\n2. VIX as a regime filter on SPY above its 200-day average")
    print(f"   trend days {len(trend_returns)}, mean {statistics.fmean(trend_returns) * 100:+.3f}%/day")
    print(f"   VIX<20 days {len(filtered_returns)}, mean "
          f"{statistics.fmean(filtered_returns) * 100:+.3f}%/day")
    low, mean = bootstrap_lower(filtered_returns)
    print(f"   filtered mean with 95% lower bound {low * 100:+.3f}% .. {mean * 100:+.3f}%")


def section_dvol() -> None:
    btc = load("btc_usd")
    dvol = load("btc_dvol")
    days = sorted(set(btc) & set(dvol))
    if len(days) < 60:
        print("\n3. DVOL: not enough overlap")
        return
    daily = [btc[days[i]] / btc[days[i - 1]] - 1 for i in range(1, len(days))]
    realised = []
    for index in range(30, len(days)):
        window = daily[index - 30:index]
        realised.append(statistics.pstdev(window) * math.sqrt(365) * 100)
    pairs = list(zip(days[30:], realised, [dvol[d] for d in days[30:]]))
    premia = [(day, dv - rv) for day, rv, dv in pairs]
    forward = []
    for day, premium in premia[:-5]:
        index = days.index(day)
        fwd = btc[days[index + 5]] / btc[day] - 1
        forward.append((premium, fwd))
    print("\n3. Deribit DVOL against 30-day realised BTC volatility")
    print(f"   days {len(premia)}, mean premium {statistics.fmean(p for _, p in premia):+.1f} vol points")
    if len(forward) > 30:
        high = [fwd for premium, fwd in forward if premium > 0]
        low = [fwd for premium, fwd in forward if premium <= 0]
        print(f"   forward 5-day BTC return when premium>0: {statistics.fmean(high) * 100:+.2f}% (n={len(high)})")
        print(f"   forward 5-day BTC return when premium<=0: {statistics.fmean(low) * 100:+.2f}% (n={len(low)})")


def section_curve() -> None:
    spy = load("spy")
    long_yield = load("yield_10y")
    short_yield = load("yield_13w")
    days = sorted(set(spy) & set(long_yield) & set(short_yield))
    inverted, normal = [], []
    for index in range(1, len(days) - 20):
        slope = long_yield[days[index]] - short_yield[days[index]]
        forward = spy[days[index + 20]] / spy[days[index]] - 1
        (inverted if slope < 0 else normal).append(forward)
    print("\n4. yield curve (10y minus 13w) and the next 20 days of SPY")
    if inverted:
        print(f"   inverted days {len(inverted)}, mean forward return "
              f"{statistics.fmean(inverted) * 100:+.2f}%")
    else:
        print("   no inverted days")
    if normal:
        print(f"   normal days   {len(normal)}, mean forward return "
              f"{statistics.fmean(normal) * 100:+.2f}%")
    else:
        print("   no normal days")


def main() -> int:
    section_gold_yield_dollar()
    section_vix_filter()
    section_dvol()
    section_curve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
