#!/usr/bin/env python3
"""Run the pre-registered gold study on the cached history.

The rules, split, costs and decision rule are fixed in docs/studies/gold_2026_11_prereg.md.
This file implements them; it must not be tuned after seeing a result.

Usage:
    python scripts/gold_study.py --venue paxg     # development window only
    python scripts/gold_study.py --venue paxg --holdout   # the one holdout look
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "agent_data" / "gold_data"
DEV_END = datetime(2024, 1, 1, tzinfo=timezone.utc)
RULES = ("ema", "donchian", "bollinger", "roc", "session", "volfilter")
COST = {"paxg": 0.002, "xauusd": 0.002}  # round trip, both sides
BOOTSTRAP = 10_000


def load(name: str) -> list[dict]:
    rows = json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))
    for row in rows:
        row["dt"] = datetime.fromtimestamp(row["t"] / 1000, tz=timezone.utc)
    return rows


def ema(values: list[float], period: int) -> list[float]:
    weight = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(value * weight + out[-1] * (1.0 - weight))
    return out


def atr(bars: list[dict], period: int = 14) -> list[float]:
    out = [0.0] * len(bars)
    for index in range(1, len(bars)):
        high, low, prev = bars[index]["h"], bars[index]["l"], bars[index - 1]["c"]
        out[index] = max(high - low, abs(high - prev), abs(low - prev))
    smoothed = [0.0] * len(bars)
    for index in range(len(bars)):
        window = out[max(0, index - period + 1):index + 1]
        smoothed[index] = sum(window) / len(window)
    return smoothed


def rolling_mean(values: list[float], period: int) -> list[float]:
    out = [0.0] * len(values)
    for index in range(len(values)):
        window = values[max(0, index - period + 1):index + 1]
        out[index] = sum(window) / len(window)
    return out


def rolling_std(values: list[float], period: int) -> list[float]:
    means = rolling_mean(values, period)
    out = [0.0] * len(values)
    for index in range(len(values)):
        window = values[max(0, index - period + 1):index + 1]
        mean = means[index]
        out[index] = math.sqrt(sum((value - mean) ** 2 for value in window) / len(window))
    return out


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * (len(ordered) - 1)))]


def signals(rule: str, bars: list[dict]) -> tuple[list[int], list[bool]]:
    """Return (entry direction per bar, exit flag per bar) evaluated at each close."""
    closes = [bar["c"] for bar in bars]
    count = len(bars)
    direction = [0] * count
    want_exit = [False] * count

    if rule in ("ema", "volfilter"):
        fast, slow = ema(closes, 50), ema(closes, 200)
        ranges = atr(bars, 14)
        for index in range(200, count):
            long = fast[index] > slow[index] and closes[index] > slow[index]
            short = fast[index] < slow[index] and closes[index] < slow[index]
            if rule == "volfilter":
                window = ranges[max(0, index - 500):index + 1]
                if len(window) > 50:
                    low, high = percentile(window, 0.25), percentile(window, 0.90)
                    if not low <= ranges[index] <= high:
                        long = short = False
            if long:
                direction[index] = 1
            elif short:
                direction[index] = -1
    elif rule == "donchian":
        for index in range(56, count):
            high = max(bar["h"] for bar in bars[index - 20:index])
            low = min(bar["l"] for bar in bars[index - 20:index])
            if closes[index] > high:
                direction[index] = 1
            elif closes[index] < low:
                direction[index] = -1
            low55 = min(bar["l"] for bar in bars[index - 55:index])
            high55 = max(bar["h"] for bar in bars[index - 55:index])
            want_exit[index] = closes[index] < low55 or closes[index] > high55
    elif rule == "bollinger":
        mid = rolling_mean(closes, 20)
        sd = rolling_std(closes, 20)
        for index in range(21, count):
            if sd[index] <= 0:
                continue
            if closes[index] < mid[index] - 2 * sd[index]:
                direction[index] = 1
            elif closes[index] > mid[index] + 2 * sd[index]:
                direction[index] = -1
            want_exit[index] = (direction[index - 1] != 0 and
                                ((closes[index] >= mid[index] and closes[index - 1] < mid[index - 1])
                                 or (closes[index] <= mid[index] and closes[index - 1] > mid[index - 1])))
    elif rule == "roc":
        for index in range(25, count):
            change = closes[index] / closes[index - 24] - 1.0
            if change >= 0.02:
                direction[index] = 1
            elif change <= -0.02:
                direction[index] = -1
    elif rule == "session":
        day_window: dict = {}
        for index, bar in enumerate(bars):
            day = bar["dt"].date()
            hour = bar["dt"].hour
            if hour < 8:
                day_window.setdefault(day, {"h": bar["h"], "l": bar["l"]})
                day_window[day]["h"] = max(day_window[day]["h"], bar["h"])
                day_window[day]["l"] = min(day_window[day]["l"], bar["l"])
            elif 8 <= hour < 20 and day in day_window:
                window = day_window[day]
                if bar["c"] > window["h"]:
                    direction[index] = 1
                elif bar["c"] < window["l"]:
                    direction[index] = -1
                if hour >= 20:
                    want_exit[index] = True
    return direction, want_exit


def simulate(rule: str, bars: list[dict], *, cost_rate: float) -> list[dict]:
    direction, want_exit = signals(rule, bars)
    ranges = atr(bars, 14)
    trades = []
    index = 200 if rule in ("ema", "volfilter") else 56
    open_trade = None
    while index < len(bars) - 1:
        if open_trade is not None:
            bar = bars[index]
            exit_price = None
            reason = ""
            if open_trade["dir"] == 1:
                if bar["l"] <= open_trade["stop"]:
                    exit_price, reason = min(bar["o"], open_trade["stop"]), "stop"
                elif bar["h"] >= open_trade["target"]:
                    exit_price, reason = open_trade["target"], "target"
            else:
                if bar["h"] >= open_trade["stop"]:
                    exit_price, reason = max(bar["o"], open_trade["stop"]), "stop"
                elif bar["l"] <= open_trade["target"]:
                    exit_price, reason = open_trade["target"], "target"
            if exit_price is None and (want_exit[index] or
                                       (rule == "roc" and index - open_trade["i"] >= 24)):
                exit_price, reason = bars[index + 1]["o"], "rule"
            if exit_price is not None:
                gross = (exit_price - open_trade["entry"]) * open_trade["dir"]
                net = gross - open_trade["entry"] * cost_rate
                trades.append({"entry_time": open_trade["time"], "exit_time": bar["dt"],
                               "dir": open_trade["dir"], "entry": open_trade["entry"],
                               "exit": exit_price, "reason": reason,
                               "r": net / open_trade["risk"]})
                open_trade = None
                index += 1
                continue
            index += 1
            continue
        if direction[index]:
            entry = bars[index + 1]["o"]
            risk = 2 * ranges[index]
            if risk <= 0:
                index += 1
                continue
            open_trade = {"dir": direction[index], "entry": entry, "risk": risk,
                          "stop": entry - risk if direction[index] == 1 else entry + risk,
                          "target": entry + 2 * risk if direction[index] == 1 else entry - 2 * risk,
                          "time": bars[index + 1]["dt"], "i": index}
        index += 1
    if open_trade is not None:
        last = bars[-1]["c"]
        gross = (last - open_trade["entry"]) * open_trade["dir"]
        net = gross - open_trade["entry"] * cost_rate
        trades.append({"entry_time": open_trade["time"], "exit_time": bars[-1]["dt"],
                       "dir": open_trade["dir"], "entry": open_trade["entry"], "exit": last,
                       "reason": "end", "r": net / open_trade["risk"]})
    return trades


def bootstrap(values: list[float], confidence: float) -> tuple[float, float]:
    generator = random.Random(20261101)
    n = len(values)
    means = sorted(sum(values[generator.randrange(n)] for _ in range(n)) / n
                   for _ in range(BOOTSTRAP))
    tail = (1.0 - confidence) / 2.0
    return means[int(tail * (BOOTSTRAP - 1))], means[int((1 - tail) * (BOOTSTRAP - 1))]


def summarise(name: str, trades: list[dict], confidence: float) -> dict:
    rs = [trade["r"] for trade in trades]
    n = len(rs)
    if n < 10:
        return {"name": name, "n": n, "mean": 0.0, "low": 0.0, "high": 0.0, "win": 0.0}
    mean = sum(rs) / n
    low, high = bootstrap(rs, confidence)
    win = sum(1 for value in rs if value > 0) / n
    return {"name": name, "n": n, "mean": mean, "low": low, "high": high, "win": win}


def run(venue: str, *, holdout: bool, confidence: float) -> int:
    files = {"paxg": ("paxg_h1", "paxg_d1"), "xauusd": ("xauusd_h1",)}
    series = files[venue]
    print(f"venue {venue}, {'HOLDOUT' if holdout else 'development'}, "
          f"confidence {confidence:.4f}")
    print(f"{'rule':10} {'n':>5} {'win%':>6} {'meanR':>8} {'lowR':>8} {'highR':>8}")
    for rule in RULES:
        trades = []
        for name in series:
            bars = load(name)
            if holdout:
                bars = [bar for bar in bars if bar["dt"] >= DEV_END]
            else:
                bars = [bar for bar in bars if bar["dt"] < DEV_END]
            if len(bars) < 500:
                continue
            trades.extend(simulate(rule, bars, cost_rate=COST[venue]))
        row = summarise(rule, trades, confidence)
        print(f"{rule:10} {row['n']:5} {row['win'] * 100:5.1f}% {row['mean']:+8.3f} "
              f"{row['low']:+8.3f} {row['high']:+8.3f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", choices=("paxg", "xauusd"), default="paxg")
    parser.add_argument("--holdout", action="store_true")
    parser.add_argument("--confidence", type=float, default=0.9958,
                        help="bootstrap confidence for the Bonferroni-corrected threshold")
    args = parser.parse_args(argv)
    return run(args.venue, holdout=args.holdout, confidence=args.confidence)


if __name__ == "__main__":
    raise SystemExit(main())
