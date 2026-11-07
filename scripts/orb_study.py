#!/usr/bin/env python3
"""Opening-range breakout on gold, the one documented family not yet tested here.

The rule is the classic one (Zarattini & Aziz style, adapted to a 24/7 market): take the
range of the first hour of the UTC day, buy a close above it, sell a close below it, stop
at the opposite side of the range, target 2x the range, close at the end of the day.
Costs are charged per side. The development window is 2020-08..2023-12; the holdout
(2024-01..2026-11) is not opened here.
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "agent_data" / "gold_data" / "paxg_h1.json"
DEV_END = datetime(2024, 1, 1, tzinfo=timezone.utc)
COST = 0.002
BOOTSTRAP = 10_000


def load() -> list[dict]:
    rows = json.loads(DATA.read_text(encoding="utf-8"))
    for row in rows:
        row["dt"] = datetime.fromtimestamp(row["t"] / 1000, tz=timezone.utc)
    return rows


def bootstrap_lower(values: list[float], confidence: float = 0.95) -> float:
    generator = random.Random(20261101)
    n = len(values)
    means = sorted(sum(values[generator.randrange(n)] for _ in range(n)) / n
                   for _ in range(BOOTSTRAP))
    return means[int((1 - confidence) * (BOOTSTRAP - 1))]


def run(rows: list[dict], *, holdout: bool) -> list[dict]:
    days: dict = {}
    for index, row in enumerate(rows):
        days.setdefault(row["dt"].date(), []).append((index, row))
    trades = []
    for day in sorted(days):
        if (day < DEV_END.date()) == holdout:
            continue
        bars = days[day]
        first_hour = [(i, b) for i, b in bars if b["dt"].hour == 0]
        if not first_hour:
            continue
        high = max(b["h"] for _, b in first_hour)
        low = min(b["l"] for _, b in first_hour)
        width = high - low
        if width <= 0:
            continue
        for index, bar in bars:
            if bar["dt"].hour < 1:
                continue
            direction = None
            if bar["c"] > high:
                direction = 1
            elif bar["c"] < low:
                direction = -1
            if direction is None:
                continue
            entry = bar["c"]
            stop = low if direction == 1 else high
            risk = abs(entry - stop)
            if risk <= 0:
                break
            target = entry + 2 * risk if direction == 1 else entry - 2 * risk
            exit_price, reason = None, ""
            for later_index, later in bars:
                if later_index <= index:
                    continue
                if direction == 1 and later["l"] <= stop:
                    exit_price, reason = stop, "stop"
                    break
                if direction == 1 and later["h"] >= target:
                    exit_price, reason = target, "target"
                    break
                if direction == -1 and later["h"] >= stop:
                    exit_price, reason = stop, "stop"
                    break
                if direction == -1 and later["l"] <= target:
                    exit_price, reason = target, "target"
                    break
            if exit_price is None:
                exit_price, reason = bars[-1][1]["c"], "close"
            gross = direction * (exit_price - entry)
            net = gross - entry * COST
            trades.append({"day": day.isoformat(), "dir": direction, "entry": entry,
                           "exit": exit_price, "reason": reason, "r": net / risk})
            break
    return trades


def main() -> int:
    rows = load()
    for holdout in (False, True):
        if holdout:
            print("holdout window is not opened by this script")
            break
        trades = run(rows, holdout=holdout)
        rs = [trade["r"] for trade in trades]
        if len(rs) < 20:
            print("too few trades")
            continue
        mean = sum(rs) / len(rs)
        low = bootstrap_lower(rs)
        wins = sum(1 for value in rs if value > 0) / len(rs)
        exits: dict[str, int] = {}
        for trade in trades:
            exits[trade["reason"]] = exits.get(trade["reason"], 0) + 1
        print(f"ORB development: {len(rs)} trades, wins {wins * 100:.1f}%, mean R {mean:+.3f}, "
              f"95% lower bound {low:+.3f}, exits {exits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
