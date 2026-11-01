#!/usr/bin/env python3
"""Evaluate the three pre-registered 2026-11 candidates on a clean development window.

The protocol lives in `docs/studies/prereg_v2_2026_11.md`; this file implements it and must
not be tuned after a development result is seen. It reads the immutable daily cache written
by `scripts/fetch_daily_basket.py` -- it never touches the terminal, so a result can be
recomputed from the manifest and the sha256s alone.

Definitions the code pins (the prose version is in the pre-registration):

* Decisions use closed bars only. A signal evaluated on bar ``i``'s close is filled at bar
  ``i+1``'s open, never at the signal bar's close.
* One position per instrument. No pyramiding, no reversal while a position is open.
* Inside a bar the protective exit is checked from the entry bar itself and **the stop is
  checked before the target**. A bar whose open is already through the stop fills at the
  open (worse than the stop); one already through the target fills at the open (better).
* R = (gross P&L - cost) / (2 x ATR(14) at the signal bar). R is net of the round-trip
  cost; the cost sweep re-scales only that subtraction, never the exits.
* The interval is a **cluster** bootstrap: a cluster is one decision date (C1, C2) or one
  calendar month (C3), so trades that share a cause are resampled together instead of
  being treated as independent draws.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "agent_data" / "daily"
LOCKS = ROOT / "agent_data" / "holdout_locks.json"

BOOTSTRAP = 10_000
SEED = 20261101
# Three candidates, one-sided 5% family-wise: 0.05 / 3 = 0.0167 -> a 98.33% interval.
DEV_CONFIDENCE = 1.0 - 0.05 / 3
HOLDOUT_CONFIDENCE = 0.90
MIN_DEV_TRADES = 100
MIN_HOLDOUT_TRADES = 30

WINDOWS = {
    "dev": (date(2004, 6, 11), date(2019, 12, 31)),
    "holdout": (date(2024, 1, 1), date(2026, 11, 1)),
}
# 2020-01-01 .. 2023-12-31 is deliberately excluded from both windows: it is the period
# earlier studies in this repository examined, so it can serve as neither clean development
# nor an unexamined holdout. It is left unused rather than folded into development.

COSTS = {
    # point = price of one MT5 point; spread_floor_points covers the D1 "spread" field being
    # a best-case quote; slippage is per side and deliberately punitive (5 bp on metals).
    "XAUUSD": {"point": 0.01, "spread_floor_points": 20, "slippage_bps": 5.0, "slippage_price": 0.0},
    "XAGUSD": {"point": 0.001, "spread_floor_points": 30, "slippage_bps": 5.0, "slippage_price": 0.0},
    "EURUSD": {"point": 0.00001, "spread_floor_points": 2, "slippage_bps": 0.0, "slippage_price": 0.00005},
    "AUDUSD": {"point": 0.00001, "spread_floor_points": 2, "slippage_bps": 0.0, "slippage_price": 0.00005},
    "NZDUSD": {"point": 0.00001, "spread_floor_points": 2, "slippage_bps": 0.0, "slippage_price": 0.00005},
}

CANDIDATES = ("c1_inside_breakout", "c2_silver_lead", "c3_fx_tsmom")

# Bars of indicator history required before a rule may fire, and the flat annual financing
# charge the protocol adds to every held night (see the pre-registration, section "Costs").
WARMUP_C1 = 30
FINANCING_PER_YEAR = {"XAUUSD": 0.03, "XAGUSD": 0.03,
                      "EURUSD": 0.02, "AUDUSD": 0.02, "NZDUSD": 0.02}


# --------------------------------------------------------------------------- data


def load(symbol: str) -> list[dict]:
    rows = json.loads((CACHE / f"{symbol}.json").read_bytes())
    for row in rows:
        row["date"] = date.fromisoformat(row["d"])
    return rows


def manifest_hash() -> str:
    return hashlib.sha256((CACHE / "manifest.json").read_bytes()).hexdigest()


# --------------------------------------------------------------------------- maths


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    centre = mean(values)
    return math.sqrt(sum((value - centre) ** 2 for value in values) / (len(values) - 1))


def atr(bars: list[dict], period: int = 14) -> list[float]:
    """Simple mean of true range over the last `period` bars, inclusive."""
    true_range = [0.0] * len(bars)
    for index in range(len(bars)):
        if index == 0:
            true_range[index] = bars[index]["h"] - bars[index]["l"]
            continue
        prev_close = bars[index - 1]["c"]
        true_range[index] = max(bars[index]["h"] - bars[index]["l"],
                                abs(bars[index]["h"] - prev_close),
                                abs(bars[index]["l"] - prev_close))
    out = [0.0] * len(bars)
    for index in range(len(bars)):
        window = true_range[max(0, index - period + 1):index + 1]
        out[index] = sum(window) / len(window)
    return out


def cost_price(symbol: str, bar: dict, price: float) -> float:
    spec = COSTS[symbol]
    spread = max(bar["spread"], spec["spread_floor_points"]) * spec["point"]
    slippage = spec["slippage_price"] + price * spec["slippage_bps"] / 10_000.0
    return spread + 2.0 * slippage


# --------------------------------------------------------------------------- exits


def protective_exit(bars: list[dict], entry_index: int, last_index: int, *, direction: int,
                    stop: float, target: float) -> tuple[int, float, str] | None:
    """First protective exit between the entry bar and `last_index`, stop before target."""
    for index in range(entry_index, last_index + 1):
        bar = bars[index]
        if direction > 0:
            if bar["o"] <= stop:
                return index, bar["o"], "stop_gap"
            if bar["l"] <= stop:
                return index, stop, "stop"
            if bar["o"] >= target:
                return index, bar["o"], "target_gap"
            if bar["h"] >= target:
                return index, target, "target"
        else:
            if bar["o"] >= stop:
                return index, bar["o"], "stop_gap"
            if bar["h"] >= stop:
                return index, stop, "stop"
            if bar["o"] <= target:
                return index, bar["o"], "target_gap"
            if bar["l"] <= target:
                return index, target, "target"
    return None


def make_trade(symbol: str, direction: int, entry_index: int, entry: float, risk: float,
               exit_index: int, exit_price: float, reason: str, bars: list[dict],
               cluster: str, cost_multiple: float = 1.0,
               financing_multiple: float = 1.0) -> dict:
    gross = (exit_price - entry) * direction
    held = exit_index - entry_index
    financing = entry * FINANCING_PER_YEAR[symbol] * held / 252.0 * financing_multiple
    cost = cost_price(symbol, bars[entry_index], entry) * cost_multiple + financing
    return {
        "symbol": symbol, "dir": direction,
        "entry_date": bars[entry_index]["date"].isoformat(),
        "exit_date": bars[exit_index]["date"].isoformat(),
        "entry": entry, "exit": exit_price, "risk": risk,
        "gross_pnl": gross, "cost": cost, "financing": financing,
        "gross_r": gross / risk, "cost_r": cost / risk, "r": (gross - cost) / risk,
        "reason": reason, "cluster": cluster, "bars_held": exit_index - entry_index + 1,
    }


# --------------------------------------------------------------------------- rules


def rule_c1(bars: list[dict]) -> dict:
    """Inside-bar compression, then a close beyond the compressed bar's range."""
    count = len(bars)
    intent = [0] * count
    for index in range(WARMUP_C1, count):
        prev, before = bars[index - 1], bars[index - 2]
        inside = prev["h"] <= before["h"] and prev["l"] >= before["l"]
        if not inside:
            continue
        if bars[index]["c"] > prev["h"]:
            intent[index] = 1
        elif bars[index]["c"] < prev["l"]:
            intent[index] = -1
    return {"intent": intent, "max_hold": 10}


def rule_c2(bars: list[dict], silver: list[dict]) -> dict:
    """Gold lags silver on a 5-day horizon; trade the standardised gap back toward zero.

    Both returns span the same pair of joint (both-venues-traded) dates, so the spread is
    a like-for-like comparison rather than two different calendars.
    """
    count = len(bars)
    intent = [0] * count
    silver_index = {bar["date"]: index for index, bar in enumerate(silver)}
    common = [(index, silver_index[bar["date"]]) for index, bar in enumerate(bars)
              if bar["date"] in silver_index]
    gaps: list[float | None] = []
    for step, (gold_at, silver_at) in enumerate(common):
        if step < 5:
            gaps.append(None)
            continue
        gold_return = bars[gold_at]["c"] / bars[common[step - 5][0]]["c"] - 1.0
        silver_return = silver[silver_at]["c"] / silver[common[step - 5][1]]["c"] - 1.0
        gaps.append(gold_return - silver_return)
    for step, (gold_at, _) in enumerate(common):
        if step < 65 or gaps[step] is None:
            continue
        window = [value for value in gaps[step - 60:step] if value is not None]
        if len(window) < 60:
            continue
        centre, scale = mean(window), stdev(window)
        if scale <= 0:
            continue
        z = (gaps[step] - centre) / scale
        if z <= -1.5:
            intent[gold_at] = 1
        elif z >= 1.5:
            intent[gold_at] = -1
    return {"intent": intent, "max_hold": 5}


def rule_c3(bars: list[dict]) -> dict:
    """Monthly time-series momentum: sign of the 252-day return, held one month."""
    count = len(bars)
    intent = [0] * count
    for index in range(1, count):
        if bars[index]["date"].month == bars[index - 1]["date"].month:
            continue
        signal = index - 1
        if signal < 252:
            continue
        change = bars[signal]["c"] / bars[signal - 252]["c"] - 1.0
        if change > 0:
            intent[signal] = 1
        elif change < 0:
            intent[signal] = -1
    return {"intent": intent}


# --------------------------------------------------------------------------- engine


def window_bounds(bars: list[dict], window: tuple[date, date]) -> tuple[int, int] | None:
    start_date, end_date = window
    indices = [index for index, bar in enumerate(bars) if start_date <= bar["date"] <= end_date]
    if len(indices) < 30:
        return None
    return indices[0], indices[-1]


def simulate(symbol: str, bars: list[dict], rule: dict, window: tuple[date, date],
             *, cost_multiple: float = 1.0, financing_multiple: float = 1.0) -> list[dict]:
    """C1/C2: next-open entry, protective exits, fixed maximum holding time."""
    bounds = window_bounds(bars, window)
    if bounds is None:
        return []
    start, end = bounds
    ranges = atr(bars, 14)
    intent = rule["intent"]
    max_hold = rule["max_hold"]
    trades: list[dict] = []
    index = start
    while index <= end:
        direction = intent[index]
        if not direction or index + 1 > end:
            index += 1
            continue
        entry_index = index + 1
        risk = 2.0 * ranges[index]
        entry = bars[entry_index]["o"]
        if risk <= 0 or entry <= 0:
            index += 1
            continue
        stop = entry - risk * direction
        target = entry + 2.0 * risk * direction
        time_exit = index + 1 + max_hold
        hit = protective_exit(bars, entry_index, min(time_exit - 1, end),
                              direction=direction, stop=stop, target=target)
        if hit is not None:
            exit_index, exit_price, reason = hit
        elif time_exit <= end:
            exit_index, exit_price, reason = time_exit, bars[time_exit]["o"], "time"
        else:
            exit_index, exit_price, reason = end, bars[end]["c"], "end"
        trades.append(make_trade(symbol, direction, entry_index, entry, risk, exit_index,
                                 exit_price, reason, bars,
                                 cluster=bars[entry_index]["date"].isoformat(),
                                 cost_multiple=cost_multiple,
                                 financing_multiple=financing_multiple))
        index = exit_index + 1
    return trades


def simulate_monthly(symbol: str, bars: list[dict], rule: dict, window: tuple[date, date],
                     *, cost_multiple: float = 1.0, financing_multiple: float = 1.0) -> list[dict]:
    """C3: enter on the first bar of a month, exit at the next month's first open."""
    bounds = window_bounds(bars, window)
    if bounds is None:
        return []
    _, end = bounds
    ranges = atr(bars, 14)
    intent = rule["intent"]
    month_starts = [index for index in range(1, len(bars))
                    if bars[index]["date"].month != bars[index - 1]["date"].month]
    trades: list[dict] = []
    for position, entry_index in enumerate(month_starts):
        if entry_index > end:
            break
        if bars[entry_index]["date"] < window[0]:
            continue
        signal = entry_index - 1
        direction = intent[signal] if signal >= 0 else 0
        if not direction or signal < 1:
            continue
        risk = 2.0 * ranges[signal]
        entry = bars[entry_index]["o"]
        if risk <= 0 or entry <= 0:
            continue
        stop = entry - risk * direction
        target = entry + 2.0 * risk * direction
        next_start = month_starts[position + 1] if position + 1 < len(month_starts) else None
        last_index = (next_start - 1) if next_start is not None else len(bars) - 1
        last_index = min(last_index, end)
        hit = protective_exit(bars, entry_index, last_index, direction=direction,
                              stop=stop, target=target)
        if hit is not None:
            exit_index, exit_price, reason = hit
        elif next_start is not None and next_start <= end:
            exit_index, exit_price, reason = next_start, bars[next_start]["o"], "rebalance"
        else:
            exit_index, exit_price, reason = last_index, bars[last_index]["c"], "end"
        trades.append(make_trade(symbol, direction, entry_index, entry, risk, exit_index,
                                 exit_price, reason, bars,
                                 cluster=f"{bars[entry_index]['date'].year}-"
                                         f"{bars[entry_index]['date'].month:02d}",
                                 cost_multiple=cost_multiple,
                                 financing_multiple=financing_multiple))
    return trades


# --------------------------------------------------------------------------- scoring


def cluster_bootstrap(trades: list[dict], confidence: float, *, rounds: int = BOOTSTRAP,
                      seed: int = SEED) -> tuple[float, float, float, float]:
    """Resample clusters, not trades. Returns (mean R, low, high, one-sided p)."""
    clusters: dict[str, list[float]] = {}
    for trade in trades:
        clusters.setdefault(trade["cluster"], []).append(trade["r"])
    keys = sorted(clusters)
    sums = [sum(clusters[key]) for key in keys]
    counts = [len(clusters[key]) for key in keys]
    total = sum(counts)
    if total == 0:
        return 0.0, 0.0, 0.0, 1.0
    observed = sum(sums) / total
    generator = random.Random(seed)
    draws: list[float] = []
    for _ in range(rounds):
        picked_sum = 0.0
        picked_count = 0
        for _ in range(len(keys)):
            choice = generator.randrange(len(keys))
            picked_sum += sums[choice]
            picked_count += counts[choice]
        draws.append(picked_sum / picked_count)
    draws.sort()
    tail = (1.0 - confidence) / 2.0
    low = draws[int(tail * (rounds - 1))]
    high = draws[int((1.0 - tail) * (rounds - 1))]
    p_value = sum(1 for value in draws if value <= 0.0) / rounds
    return observed, low, high, p_value


def summarise(name: str, trades: list[dict], confidence: float) -> dict:
    values = [trade["r"] for trade in trades]
    count = len(values)
    if count == 0:
        return {"name": name, "n": 0, "mean": 0.0, "low": 0.0, "high": 0.0, "p": 1.0,
                "win": 0.0, "t": 0.0, "profit_factor": 0.0, "max_dd_r": 0.0,
                "gross_mean": 0.0, "cost_r": 0.0, "reasons": {}, "by_symbol": {}}
    observed, low, high, p_value = cluster_bootstrap(trades, confidence)
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value <= 0]
    equity = peak = drawdown = 0.0
    for trade in trades:
        equity += trade["r"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    deviation = stdev(values)
    return {
        "name": name, "n": count,
        "mean": observed, "low": low, "high": high, "p": p_value,
        "win": len(wins) / count,
        "t": observed / (deviation / math.sqrt(count)) if deviation > 0 else 0.0,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf"),
        "max_dd_r": drawdown,
        "gross_mean": mean([trade["gross_r"] for trade in trades]),
        "cost_r": mean([trade["cost_r"] for trade in trades]),
        "reasons": reason_histogram(trades),
        "by_symbol": per_symbol(trades),
    }


def reason_histogram(trades: list[dict]) -> dict:
    out: dict[str, int] = {}
    for trade in trades:
        out[trade["reason"]] = out.get(trade["reason"], 0) + 1
    return dict(sorted(out.items()))


def per_symbol(trades: list[dict]) -> dict:
    symbols: dict[str, list[float]] = {}
    for trade in trades:
        symbols.setdefault(trade["symbol"], []).append(trade["r"])
    return {symbol: {"n": len(values), "mean": mean(values)}
            for symbol, values in sorted(symbols.items())}


# --------------------------------------------------------------------------- study


def pieces(candidate: str):
    if candidate == "c1_inside_breakout":
        bars = load("XAUUSD")
        return [("XAUUSD", bars, rule_c1(bars), simulate)]
    if candidate == "c2_silver_lead":
        gold, silver = load("XAUUSD"), load("XAGUSD")
        return [("XAUUSD", gold, rule_c2(gold, silver), simulate)]
    if candidate == "c3_fx_tsmom":
        out = []
        for symbol in ("EURUSD", "AUDUSD", "NZDUSD"):
            bars = load(symbol)
            out.append((symbol, bars, rule_c3(bars), simulate_monthly))
        return out
    raise SystemExit(f"unknown candidate {candidate}")


def build(candidate: str, window: str, *, cost_multiple: float = 1.0,
          financing_multiple: float = 1.0) -> list[dict]:
    start, end = WINDOWS[window]
    trades: list[dict] = []
    for symbol, bars, rule, runner in pieces(candidate):
        trades.extend(runner(symbol, bars, rule, (start, end), cost_multiple=cost_multiple,
                             financing_multiple=financing_multiple))
    return trades


def cost_sweep(candidate: str, window: str) -> dict:
    """Re-price the same trades at 0x and 2x cost (exits never depend on the cost)."""
    out = {}
    rows = (("zero", 0.0, 0.0), ("transaction_only", 1.0, 0.0),
            ("base", 1.0, 1.0), ("double", 2.0, 2.0))
    for label, transaction, financing in rows:
        values = [trade["r"] for trade in build(candidate, window, cost_multiple=transaction,
                                                financing_multiple=financing)]
        out[label] = {"n": len(values), "mean": mean(values)}
    return out


def verdict(summary: dict, window: str) -> dict:
    floor = MIN_DEV_TRADES if window == "dev" else MIN_HOLDOUT_TRADES
    confidence = DEV_CONFIDENCE if window == "dev" else HOLDOUT_CONFIDENCE
    if summary["n"] < floor:
        reason = f"n={summary['n']} is below the pre-registered floor of {floor}; no decision"
    elif summary["low"] > 0:
        reason = (f"mean net R {summary['mean']:+.4f}, {confidence:.2%} interval "
                  f"[{summary['low']:+.4f}, {summary['high']:+.4f}] entirely above zero")
    elif summary["high"] < 0:
        reason = (f"mean net R {summary['mean']:+.4f}, {confidence:.2%} interval "
                  f"[{summary['low']:+.4f}, {summary['high']:+.4f}] entirely below zero")
    else:
        reason = (f"mean net R {summary['mean']:+.4f}, {confidence:.2%} interval "
                  f"[{summary['low']:+.4f}, {summary['high']:+.4f}] straddles zero")
    return {"passed": summary["n"] >= floor and summary["low"] > 0,
            "threshold_n": floor, "confidence": confidence, "reason": reason}


def holdout_lock(candidate: str) -> None:
    locks = json.loads(LOCKS.read_text(encoding="utf-8")) if LOCKS.exists() else {}
    if candidate in locks:
        raise SystemExit(f"holdout already opened for {candidate} at {locks[candidate]}; "
                         "a holdout is opened exactly once")
    locks[candidate] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    LOCKS.parent.mkdir(parents=True, exist_ok=True)
    LOCKS.write_text(json.dumps(locks, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", default="all", choices=("all",) + CANDIDATES)
    parser.add_argument("--window", default="dev", choices=("dev", "holdout"))
    parser.add_argument("--json", default="")
    parser.add_argument("--cost-sweep", action="store_true")
    args = parser.parse_args(argv)

    wanted = CANDIDATES if args.candidate == "all" else (args.candidate,)
    if args.window == "holdout":
        if len(wanted) != 1:
            raise SystemExit("the holdout is opened one candidate at a time, after it passed development")
        holdout_lock(wanted[0])

    confidence = DEV_CONFIDENCE if args.window == "dev" else HOLDOUT_CONFIDENCE
    report = {"window": args.window, "confidence": confidence, "seed": SEED,
              "bootstrap_rounds": BOOTSTRAP, "cache_manifest_sha256": manifest_hash(),
              "windows": {name: [low.isoformat(), high.isoformat()]
                          for name, (low, high) in WINDOWS.items()},
              "candidates": {}}
    print(f"window {args.window} ({WINDOWS[args.window][0]} .. {WINDOWS[args.window][1]}), "
          f"confidence {confidence:.4f}, cache {manifest_hash()[:12]}")
    print(f"{'candidate':22}{'n':>6}{'win%':>7}{'meanR':>9}{'lowR':>9}{'highR':>9}{'p':>8}{'costR':>8}")
    for candidate in wanted:
        summary = summarise(candidate, build(candidate, args.window), confidence)
        decision = verdict(summary, args.window)
        summary["decision"] = decision
        if args.cost_sweep:
            summary["cost_sweep"] = cost_sweep(candidate, args.window)
        report["candidates"][candidate] = summary
        print(f"{candidate:22}{summary['n']:6}{summary['win'] * 100:6.1f}%"
              f"{summary['mean']:+9.4f}{summary['low']:+9.4f}{summary['high']:+9.4f}"
              f"{summary['p']:8.4f}{summary['cost_r']:+8.4f}")
        print(f"    {decision['reason']}")
        print(f"    exits {summary['reasons']}  by symbol {summary['by_symbol']}")
        if args.cost_sweep:
            print(f"    cost sweep {summary['cost_sweep']}")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"evidence: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
