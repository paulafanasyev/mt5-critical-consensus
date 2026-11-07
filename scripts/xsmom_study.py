#!/usr/bin/env python3
"""Cross-sectional momentum on the newly downloaded universe.

The classic 12-1 rule (Jegadeesh & Titman): rank assets by their return over the past
twelve months excluding the most recent one, hold the top half for a month, repeat.
Costs are charged on every rebalance. The purpose is to see whether the documented
effect exists on this universe after costs, not to promise it does.
"""
from __future__ import annotations

import json
import random
import statistics
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "agent_data" / "market_data"
UNIVERSE = ["spy", "qqq", "gld", "slv", "tlt", "eem", "oil_futures"]
COST_PER_SIDE = 0.001
BOOTSTRAP = 10_000


def load(name: str) -> dict[str, float]:
    payload = json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))
    return {datetime.fromtimestamp(bar["t"] / 1000, tz=timezone.utc).date().isoformat(): bar["c"]
            for bar in payload["bars"]}


def main() -> int:
    series = {name: load(name) for name in UNIVERSE}
    days = sorted(set.intersection(*(set(value) for value in series.values())))
    print(f"universe {len(UNIVERSE)} assets, {len(days)} common days {days[0]} .. {days[-1]}")

    # One observation per month: the last common day of each month.
    months: dict[str, str] = {}
    for day in days:
        months[day[:7]] = day
    month_ends = [months[key] for key in sorted(months)]

    returns = []
    for index in range(13, len(month_ends) - 1):
        entry_day = month_ends[index]
        exit_day = month_ends[index + 1]
        scores = {}
        for name, prices in series.items():
            past = prices.get(month_ends[index - 12])
            recent = prices.get(month_ends[index - 1])
            if past and recent:
                scores[name] = recent / past - 1
        if len(scores) < 4:
            continue
        ranked = sorted(scores, key=scores.get, reverse=True)
        longs, shorts = ranked[:len(ranked) // 2], ranked[len(ranked) // 2:]
        gross = 0.0
        for name in longs:
            gross += series[name][exit_day] / series[name][entry_day] - 1
        for name in shorts:
            gross -= series[name][exit_day] / series[name][entry_day] - 1
        gross /= len(longs)
        cost = COST_PER_SIDE * 2 * (len(longs) + len(shorts)) / max(1, len(longs))
        returns.append(gross - cost)

    n = len(returns)
    mean = statistics.fmean(returns)
    sd = statistics.pstdev(returns) / max(1, n) ** 0.5
    generator = random.Random(20261101)
    means = sorted(sum(returns[generator.randrange(n)] for _ in range(n)) / n
                   for _ in range(BOOTSTRAP))
    low = means[int(0.05 * (BOOTSTRAP - 1))]
    wins = sum(1 for value in returns if value > 0) / n
    print(f"monthly rebalances {n}, wins {wins * 100:.1f}%, mean {mean * 100:+.2f}% per month, "
          f"95% lower bound {low * 100:+.2f}%, t = {mean / sd:.2f}")
    print("verdict:", "candidate" if low > 0 else "no edge after costs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
