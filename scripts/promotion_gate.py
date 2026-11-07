"""Promotion gate: which demo scheme has earned the right to trade real money.

Reads the closed deals of the lab swarm from the terminal, groups them by agent magic, and
prints for each scheme the statistics the protocol requires: trade count, mean net R,
bootstrap confidence interval, profit factor and the worst drawdown of the R curve.

A scheme passes only when it has at least MIN_TRADES, a 95% interval entirely above zero,
and a drawdown inside MAX_DRAWDOWN_R.
"""
from __future__ import annotations

import argparse
import math
import random
import statistics
from datetime import datetime, timedelta, timezone

MIN_TRADES = 100
MAX_DRAWDOWN_R = 15.0


def bootstrap_lower(values: list[float], confidence: float = 0.95, rounds: int = 5000) -> float:
    generator = random.Random(20261102)
    n = len(values)
    means = sorted(sum(values[generator.randrange(n)] for _ in range(n)) / n
                   for _ in range(rounds))
    return means[int((1 - confidence) * (rounds - 1))]


def main() -> int:
    import MetaTrader5 as mt5

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    if not mt5.initialize():
        raise SystemExit("MetaTrader5.initialize failed")

    end = datetime.now() + timedelta(days=1)
    start = datetime.now() - timedelta(days=args.days)
    deals = mt5.history_deals_get(start, end) or []
    mt5.shutdown()

    by_magic: dict[int, list[float]] = {}
    for deal in deals:
        if deal.magic < 8180000 or deal.entry not in (1, 2, 3):
            continue
        by_magic.setdefault(deal.magic, []).append(deal.profit)

    print(f"сделок лаборатории за {args.days} дней: "
          f"{sum(len(v) for v in by_magic.values())}, схем: {len(by_magic)}")
    print(f"{'magic':8} {'trades':>7} {'mean $':>9} {'win%':>6} {'maxDD $':>10} {'gate':>10}")
    passed = []
    for magic, profits in sorted(by_magic.items(), key=lambda kv: -len(kv[1])):
        n = len(profits)
        mean = statistics.fmean(profits)
        wins = sum(1 for value in profits if value > 0) / n
        equity = peak = dd = 0.0
        for value in profits:
            equity += value
            peak = max(peak, equity)
            dd = max(dd, peak - equity)
        low = bootstrap_lower(profits) if n >= 20 else float("-inf")
        gate = "PASS" if (n >= MIN_TRADES and low > 0 and dd <= MAX_DRAWDOWN_R) else "lab"
        if gate == "PASS":
            passed.append(magic)
        print(f"{magic:8} {n:7} {mean:>+9.2f} {wins * 100:>5.0f}% {dd:>10.2f} {gate:>10}"
              f"   lower bound {low:+.2f}")
    print(f"\nпрошли ворота Stage 1: {passed or 'ни одна схема пока'}")
    print("Для Stage 2 нужен теневой прогон на реальной площадке; "
          "см. docs/studies/demo_to_real_protocol.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
