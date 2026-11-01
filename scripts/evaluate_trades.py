#!/usr/bin/env python3
"""Evaluate any trade list: expectancy, confidence interval, drawdown and ruin risk.

The point is to answer the operator's question — "with what probability do we win and
with what probability do we lose" — for any system, including a bot found on GitHub.
Feed it a CSV with a single column (r, or pnl) and it reports the distribution, not a
promise.

Usage:
    python scripts/evaluate_trades.py --csv trades.csv --risk 0.01 --ruin 0.5
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from pathlib import Path

ROUNDS = 10_000


def load(path: Path) -> list[float]:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return [float(row.get("r", row.get("pnl"))) for row in data]
        return [float(value) for value in data]
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        column = "r" if "r" in (reader.fieldnames or []) else "pnl"
        return [float(row[column]) for row in reader if row.get(column) not in (None, "")]


def bootstrap(values: list[float], confidence: float, seed: int = 20261101) -> tuple[float, float]:
    generator = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[generator.randrange(n)] for _ in range(n)) / n
                   for _ in range(ROUNDS))
    tail = (1 - confidence) / 2
    return means[int(tail * (ROUNDS - 1))], means[int((1 - tail) * (ROUNDS - 1))]


def ruin_probability(values: list[float], *, risk: float, ruin: float,
                     trades: int, paths: int = 2_000, seed: int = 7) -> float:
    """Share of simulated sequences whose equity falls by `ruin` before the end."""
    generator = random.Random(seed)
    n = len(values)
    ruined = 0
    for _ in range(paths):
        equity = 1.0
        for _ in range(trades):
            equity *= 1.0 + risk * values[generator.randrange(n)]
            if equity <= 1.0 - ruin:
                ruined += 1
                break
    return ruined / paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="CSV/JSON of per-trade r or pnl")
    parser.add_argument("--risk", type=float, default=0.01, help="risk per trade, fraction")
    parser.add_argument("--ruin", type=float, default=0.5, help="ruin threshold, fraction of equity")
    parser.add_argument("--horizon", type=int, default=200, help="trades to simulate for ruin")
    parser.add_argument("--confidence", type=float, default=0.90)
    args = parser.parse_args(argv)

    values = load(Path(args.csv))
    n = len(values)
    if n < 20:
        print(f"{n} trades — INSUFFICIENT (need at least 20 to say anything)")
        return 2

    wins = [value for value in values if value > 0]
    losses = [value for value in values if value <= 0]
    mean = sum(values) / n
    low, high = bootstrap(values, args.confidence)
    win_rate = len(wins) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_win, gross_loss = sum(wins), -sum(losses)
    profit_factor = gross_win / gross_loss if gross_loss else float("inf")

    equity, peak, max_dd, streak, worst_streak = 0.0, 0.0, 0.0, 0, 0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        streak = streak + 1 if value <= 0 else 0
        worst_streak = max(worst_streak, streak)

    ruin = ruin_probability(values, risk=args.risk, ruin=args.ruin, trades=args.horizon)

    print(f"trades {n}")
    print(f"  win rate        {win_rate * 100:6.1f}%")
    print(f"  average win     {avg_win:+.3f}R      average loss {avg_loss:+.3f}R")
    print(f"  expectancy      {mean:+.4f}R per trade   90% CI [{low:+.3f}, {high:+.3f}]")
    print(f"  profit factor   {profit_factor:6.2f}")
    print(f"  max drawdown    {max_dd:6.2f}R     longest losing streak {worst_streak}")
    print(f"  ruin risk       {ruin * 100:5.1f}% over {args.horizon} trades at "
          f"{args.risk * 100:.1f}% risk, ruin at -{args.ruin * 100:.0f}%")

    if low > 0 and ruin < 0.05:
        print("VERDICT: candidate — positive expectancy with a confidence interval above zero")
        return 0
    if high < 0:
        print("VERDICT: negative — the interval is entirely below zero; do not trade it")
        return 1
    print("VERDICT: inconclusive — the interval straddles zero; more trades, not more analysis")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
