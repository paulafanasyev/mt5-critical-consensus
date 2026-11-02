#!/usr/bin/env python3
"""Run the pre-registered gold/silver ratio study.

Rule, split, costs and decisions are fixed in docs/studies/ratio_2026_11_prereg.md.
Do not tune after seeing a result.
"""
from __future__ import annotations

import argparse
import math
import random
from datetime import datetime, timezone

DEV_END = datetime(2025, 7, 1, tzinfo=timezone.utc)
HOLDOUT_START = DEV_END
BOOTSTRAP = 10_000
COST = 0.003  # 0.15% per leg round trip, two legs
WINDOW = 100
ENTRY_Z = 2.0
STOP_Z = 3.5
MAX_HOURS = 120


def fetch(symbol: str, bars: int) -> dict[int, float]:
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise SystemExit("MetaTrader5.initialize failed")
    try:
        mt5.symbol_select(symbol, True)
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, bars)
    finally:
        mt5.shutdown()
    return {int(rate["time"]): float(rate["close"]) for rate in rates}


def zscores(ratios: list[float], window: int = WINDOW) -> list[float]:
    out = [0.0] * len(ratios)
    for index in range(window, len(ratios)):
        chunk = ratios[index - window + 1:index + 1]
        mean = sum(chunk) / len(chunk)
        std = math.sqrt(sum((value - mean) ** 2 for value in chunk) / len(chunk))
        out[index] = (ratios[index] - mean) / std if std > 0 else 0.0
    return out


def simulate(times: list[int], ratios: list[float], z: list[float], *,
             start: int, end: int) -> list[dict]:
    trades = []
    index = max(start, WINDOW + 1)
    while index < end - 1:
        if abs(z[index]) < ENTRY_Z:
            index += 1
            continue
        direction = 1 if z[index] <= -ENTRY_Z else -1
        entry_index = index + 1
        entry = ratios[entry_index]
        window = ratios[max(0, index - WINDOW + 1):index + 1]
        mean = sum(window) / len(window)
        std = math.sqrt(sum((value - mean) ** 2 for value in window) / len(window))
        # Risk in price terms: 1.5 standard deviations from a 2-sigma entry to a 3.5-sigma stop.
        risk = (1.5 * std) / entry if std > 0 else 0.0
        exit_index = None
        for offset in range(entry_index, min(end, entry_index + MAX_HOURS)):
            crossed = (direction == 1 and z[offset] >= 0) or (direction == -1 and z[offset] <= 0)
            if crossed or abs(z[offset]) >= STOP_Z:
                exit_index = offset
                break
        if exit_index is None:
            exit_index = min(end - 1, entry_index + MAX_HOURS)
        exit_ratio = ratios[exit_index]
        gross = direction * (exit_ratio / entry - 1.0)
        net = gross - COST
        trades.append({"time": times[entry_index], "dir": direction, "entry": entry,
                       "exit": exit_ratio, "net": net,
                       "r": net / risk if risk > 0 else 0.0})
        index = exit_index + 1
    return trades


def bootstrap_lower(values: list[float], confidence: float = 0.95) -> float:
    generator = random.Random(20261101)
    n = len(values)
    means = sorted(sum(values[generator.randrange(n)] for _ in range(n)) / n
                   for _ in range(BOOTSTRAP))
    return means[int((1 - confidence) * (BOOTSTRAP - 1))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=int, default=20000)
    parser.add_argument("--holdout", action="store_true")
    args = parser.parse_args(argv)

    gold = fetch("XAUUSD", args.bars)
    silver = fetch("XAGUSD", args.bars)
    common = sorted(set(gold) & set(silver))
    times = common
    ratios = [gold[t] / silver[t] for t in times]
    z = zscores(ratios)

    start = 0
    end = len(times)
    if args.holdout:
        start = next((i for i, t in enumerate(times)
                      if datetime.fromtimestamp(t, tz=timezone.utc) >= HOLDOUT_START), len(times))
    else:
        end = next((i for i, t in enumerate(times)
                    if datetime.fromtimestamp(t, tz=timezone.utc) >= DEV_END), len(times))

    trades = simulate(times, ratios, z, start=start, end=end)
    rs = [trade["r"] for trade in trades]
    window = "HOLDOUT" if args.holdout else "development"
    first = datetime.fromtimestamp(times[start], tz=timezone.utc)
    last = datetime.fromtimestamp(times[end - 1], tz=timezone.utc)
    print(f"ratio study {window}: {len(times)} aligned hours ({first:%Y-%m-%d} .. {last:%Y-%m-%d}), "
          f"{len(rs)} trades")
    if len(rs) < 10:
        print("too few trades to judge")
        return 2
    mean = sum(rs) / len(rs)
    low = bootstrap_lower(rs)
    wins = sum(1 for value in rs if value > 0) / len(rs)
    equity, peak, dd = 0.0, 0.0, 0.0
    for value in rs:
        equity += value
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    needed = 30 if args.holdout else 40
    passed = low > 0 and len(rs) >= needed
    print(f"  win rate {wins * 100:5.1f}%  mean R {mean:+.4f}  95% lower bound {low:+.4f}")
    print(f"  max drawdown {dd:.2f}R   verdict: {'PASS' if passed else 'FAIL / INSUFFICIENT'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
