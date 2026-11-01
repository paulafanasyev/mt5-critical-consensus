#!/usr/bin/env python3
"""Calendar effects on gold: turn-of-month and day-of-week, with costs and intervals.

These are among the oldest documented anomalies. The test is deliberately simple: a
position held for a fixed calendar window, no stop, return measured in percent, with the
round-trip cost subtracted, and a bootstrap interval. If the mean return does not clear
zero after costs, the anomaly is not tradable at retail cost, whatever its p-value in the
literature.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "agent_data" / "gold_data"
COST_ROUND_TRIP = 0.002
ROUNDS = 10_000


def load_daily(name: str) -> list[dict]:
    rows = json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))
    for row in rows:
        row["dt"] = datetime.fromtimestamp(row["t"] / 1000, tz=timezone.utc)
    return rows


def bootstrap(values: list[float], confidence: float = 0.90) -> tuple[float, float]:
    generator = random.Random(20261101)
    n = len(values)
    means = sorted(sum(values[generator.randrange(n)] for _ in range(n)) / n
                   for _ in range(ROUNDS))
    tail = (1 - confidence) / 2
    return means[int(tail * (ROUNDS - 1))], means[int((1 - tail) * (ROUNDS - 1))]


def describe(label: str, returns: list[float]) -> None:
    if len(returns) < 20:
        print(f"{label:26} n={len(returns):4} too few")
        return
    mean = statistics.fmean(returns)
    low, high = bootstrap(returns)
    wins = sum(1 for value in returns if value > 0) / len(returns)
    print(f"{label:26} n={len(returns):4} mean {mean * 100:+6.3f}% "
          f"CI [{low * 100:+6.3f}%, {high * 100:+6.3f}%] wins {wins * 100:4.1f}%")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="paxg_d1")
    parser.add_argument("--cost", type=float, default=COST_ROUND_TRIP)
    args = parser.parse_args(argv)

    bars = load_daily(args.name)
    print(f"{args.name}: {len(bars)} daily bars, cost {args.cost * 100:.2f}% round trip")

    # Turn of month: from the last close of a month to the close three trading days later.
    tom = []
    for index in range(1, len(bars) - 3):
        previous, current = bars[index - 1]["dt"], bars[index]["dt"]
        if current.month != previous.month:
            entry = bars[index]["o"]
            exit_price = bars[index + 2]["c"]
            tom.append(exit_price / entry - 1 - args.cost)
    describe("turn of month (3 days)", tom)

    # Day of week: the return of each weekday's bar. A weekday rule holds one day but pays
    # a round trip once per week, so the per-observation cost is one fifth of the round trip.
    by_day: dict[int, list[float]] = {}
    for index in range(1, len(bars)):
        change = bars[index]["c"] / bars[index - 1]["c"] - 1 - args.cost / 5.0
        by_day.setdefault(bars[index]["dt"].weekday(), []).append(change)
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for day in sorted(by_day):
        describe(f"{names[day]} daily return", by_day[day])

    # Month of year: mean daily return per month, to show how easily a "seasonal" appears.
    by_month: dict[int, list[float]] = {}
    for index in range(1, len(bars)):
        change = bars[index]["c"] / bars[index - 1]["c"] - 1 - args.cost / 5.0
        by_month.setdefault(bars[index]["dt"].month, []).append(change)
    print("monthly means (daily returns, %):")
    for month in sorted(by_month):
        values = by_month[month]
        print(f"  {month:02d}: {statistics.fmean(values) * 100:+6.3f}%  n={len(values)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
