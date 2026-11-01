#!/usr/bin/env python3
"""Evaluate the three pre-registered 2026-11 candidates on a clean development window.

The protocol lives in `docs/studies/prereg_v2_2026_11.md`; this file implements it and must
not be tuned after a development result is seen. It reads the immutable daily cache written
by `scripts/fetch_daily_basket.py` -- it never touches the terminal, so a result can be
recomputed from the manifest and the sha256s alone.

Definitions the code pins (the prose version is in the pre-registration):

* Decisions use closed bars only. A signal evaluated on bar ``i``'s close is filled at bar
  ``i+1``'s open, never at the signal bar's close.
* One position per instrument. No pyramiding, no reversal, and no new signal is evaluated on
  the bar on which a position is closed.
* Protective exits are checked from the entry bar itself. A gap through a level fills at the
  open on either side; only when the open is inside both levels does the intrabar rule apply,
  and then the stop is checked before the target.
* ``max_hold`` counts the entry bar as the first held bar: protections are live on bars
  ``E .. E+max_hold-1``, and the time exit is the open of ``E+max_hold``.
* R = (gross P&L - transaction cost - financing) / (2 x ATR(14) at the signal bar). R is
  therefore net of every charge.
* The interval is a **moving-block** bootstrap over entry months: three consecutive months
  are drawn together, so positions that share a month (three correlated FX pairs) and
  positions in adjacent months (one dislocation, one regime) are resampled as a block.
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
LOCKS = ROOT / "docs" / "evidence" / "holdout_locks.json"
DEV_EVIDENCE = ROOT / "docs" / "evidence" / "candidates_dev_2026_11.json"

BOOTSTRAP = 10_000
SEED = 20261101
# Three candidates. A two-sided 98.33% interval leaves 0.83% in each tail, so each candidate
# is a one-sided 0.83% test and the family-wise error is at most 2.5% -- stricter than the
# 5% the protocol allows, and kept as registered.
DEV_CONFIDENCE = 1.0 - 0.05 / 3
HOLDOUT_CONFIDENCE = 0.90
MIN_DEV_TRADES = 100
MIN_HOLDOUT_TRADES = 30
MIN_CLUSTERS = 30
BLOCK_LENGTH = 3

WINDOWS = {
    "dev": (date(2004, 6, 11), date(2019, 12, 31)),
    "holdout": (date(2024, 1, 1), date(2026, 11, 1)),
}
# 2020-01-01 .. 2023-12-31 is excluded from both windows: earlier studies in this repository
# examined it, so it is neither clean development nor an unexamined holdout. It may still be
# read *backwards* by an indicator lookback that starts at a window edge (e.g. the 252-day
# return on the first holdout month); it can never contribute a trade of its own.

COSTS = {
    # point = price of one MT5 point; spread_floor_points covers the D1 "spread" field being
    # a best-case quote; slippage is per side and deliberately punitive (5 bp on metals).
    "XAUUSD": {"point": 0.01, "spread_floor_points": 20, "slippage_bps": 5.0, "slippage_price": 0.0},
    "XAGUSD": {"point": 0.001, "spread_floor_points": 30, "slippage_bps": 5.0, "slippage_price": 0.0},
    "EURUSD": {"point": 0.00001, "spread_floor_points": 2, "slippage_bps": 0.0, "slippage_price": 0.00005},
    "AUDUSD": {"point": 0.00001, "spread_floor_points": 2, "slippage_bps": 0.0, "slippage_price": 0.00005},
    "NZDUSD": {"point": 0.00001, "spread_floor_points": 2, "slippage_bps": 0.0, "slippage_price": 0.00005},
}

CANDIDATES = ("c1_inside_breakout", "c2_ratio_reversion", "c3_fx_tsmom")

# Bars of indicator history required before a rule may fire, and the flat annual financing
# charge the protocol adds to every held night (see the pre-registration, section "Costs").
WARMUP_C1 = 30
WARMUP_C3 = 252
FINANCING_PER_YEAR = {"XAUUSD": 0.03, "XAGUSD": 0.03,
                      "EURUSD": 0.02, "AUDUSD": 0.02, "NZDUSD": 0.02}


# --------------------------------------------------------------------------- data


def load(symbol: str) -> list[dict]:
    path = CACHE / f"{symbol}.json"
    try:
        payload = path.read_bytes()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Missing frozen MT5 research data for {symbol}: {path}. "
            "The candidate study requires the original broker cache; on the MT5 host run "
            "'python scripts/fetch_daily_basket.py'. Never substitute synthetic test data."
        ) from exc
    rows = json.loads(payload)
    for row in rows:
        row["date"] = date.fromisoformat(row["d"])
    return rows


def manifest_hash() -> str:
    return hashlib.sha256((CACHE / "manifest.json").read_bytes()).hexdigest()


def engine_hash() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


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
    """First protective exit between the entry bar and `last_index`.

    A gap through either level fills at that bar's open, so both opens are checked before
    any intrabar level; only inside the bar does the stop win over the target.
    """
    for index in range(entry_index, last_index + 1):
        bar = bars[index]
        if direction > 0:
            if bar["o"] <= stop:
                return index, bar["o"], "stop_gap"
            if bar["o"] >= target:
                return index, bar["o"], "target_gap"
            if bar["l"] <= stop:
                return index, stop, "stop"
            if bar["h"] >= target:
                return index, target, "target"
        else:
            if bar["o"] >= stop:
                return index, bar["o"], "stop_gap"
            if bar["o"] <= target:
                return index, bar["o"], "target_gap"
            if bar["h"] >= stop:
                return index, stop, "stop"
            if bar["l"] <= target:
                return index, target, "target"
    return None


def month_key(day: date) -> str:
    return f"{day.year}-{day.month:02d}"


def make_trade(symbol: str, direction: int, entry_index: int, entry: float, risk: float,
               exit_index: int, exit_price: float, reason: str, bars: list[dict],
               cost_multiple: float = 1.0, financing_multiple: float = 1.0) -> dict:
    gross = (exit_price - entry) * direction
    nights = exit_index - entry_index
    financing = entry * FINANCING_PER_YEAR[symbol] * nights / 252.0 * financing_multiple
    transaction = cost_price(symbol, bars[entry_index], entry) * cost_multiple
    cost = transaction + financing
    return {
        "symbol": symbol, "dir": direction,
        "entry_date": bars[entry_index]["date"].isoformat(),
        "exit_date": bars[exit_index]["date"].isoformat(),
        "entry": entry, "exit": exit_price, "risk": risk,
        "gross_pnl": gross, "transaction": transaction, "financing": financing,
        "cost": cost, "gross_r": gross / risk, "cost_r": cost / risk,
        "r": (gross - cost) / risk, "reason": reason,
        "cluster": month_key(bars[entry_index]["date"]),
        "bars_held": exit_index - entry_index + 1, "financing_nights": nights,
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
    """Gold/silver ratio reversion: trade the standardised 5-day relative return back to zero.

    Both returns span the same pair of joint (both-venues-traded) dates, so the spread is a
    like-for-like comparison rather than two different calendars.
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
        if signal < WARMUP_C3:
            continue
        change = bars[signal]["c"] / bars[signal - WARMUP_C3]["c"] - 1.0
        if change > 0:
            intent[signal] = 1
        elif change < 0:
            intent[signal] = -1
    return {"intent": intent}


# --------------------------------------------------------------------------- engine


def window_bounds(bars: list[dict], window: tuple[date, date]) -> tuple[int, int]:
    start_date, end_date = window
    indices = [index for index, bar in enumerate(bars) if start_date <= bar["date"] <= end_date]
    if len(indices) < 30:
        raise ValueError(f"window {window} contains only {len(indices)} bars; refusing to score it")
    return indices[0], indices[-1]


def simulate(symbol: str, bars: list[dict], rule: dict, window: tuple[date, date],
             *, cost_multiple: float = 1.0, financing_multiple: float = 1.0) -> list[dict]:
    """C1/C2: next-open entry, protective exits, fixed maximum holding time."""
    start, end = window_bounds(bars, window)
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
                                 cost_multiple=cost_multiple,
                                 financing_multiple=financing_multiple))
        # The exit bar's own close is not evaluated: the next candidate signal is the bar
        # after the exit, filled at the open of the bar after that.
        index = exit_index + 1
    return trades


def simulate_monthly(symbol: str, bars: list[dict], rule: dict, window: tuple[date, date],
                     *, cost_multiple: float = 1.0, financing_multiple: float = 1.0) -> list[dict]:
    """C3: enter on the first bar of a month, exit at the next month's first open."""
    _, end = window_bounds(bars, window)
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
                                 cost_multiple=cost_multiple,
                                 financing_multiple=financing_multiple))
    return trades


# --------------------------------------------------------------------------- scoring


def cluster_bootstrap(trades: list[dict], confidence: float, *, rounds: int = BOOTSTRAP,
                      seed: int = SEED, block: int = BLOCK_LENGTH) -> tuple[float, float, float]:
    """Moving-block bootstrap over entry months. Returns (mean R, low, high).

    Months are ordered chronologically and ``block`` consecutive months are drawn per step
    with wrap-around, so a block carries both contemporaneous trades (three FX pairs in one
    month) and the serial dependence between adjacent months. With ``block == len(months)``
    every draw is the whole sample, which the test suite uses to prove the block mechanism.
    """
    clusters: dict[str, list[float]] = {}
    for trade in trades:
        clusters.setdefault(trade["cluster"], []).append(trade["r"])
    keys = sorted(clusters)
    sums = [sum(clusters[key]) for key in keys]
    counts = [len(clusters[key]) for key in keys]
    total = sum(counts)
    if total == 0:
        return 0.0, 0.0, 0.0
    observed = sum(sums) / total
    width = max(1, min(block, len(keys)))
    generator = random.Random(seed)
    draws: list[float] = []
    for _ in range(rounds):
        picked_sum = 0.0
        picked_count = 0
        while picked_count < total:
            start = generator.randrange(len(keys))
            for offset in range(width):
                choice = (start + offset) % len(keys)
                picked_sum += sums[choice]
                picked_count += counts[choice]
        draws.append(picked_sum / picked_count)
    draws.sort()
    tail = (1.0 - confidence) / 2.0
    return observed, draws[int(tail * (rounds - 1))], draws[int((1.0 - tail) * (rounds - 1))]


def summarise(name: str, trades: list[dict], confidence: float) -> dict:
    values = [trade["r"] for trade in trades]
    count = len(values)
    clusters = len({trade["cluster"] for trade in trades})
    if count == 0:
        return {"name": name, "n": 0, "clusters": 0, "mean": 0.0, "low": 0.0, "high": 0.0,
                "win": 0.0, "profit_factor": 0.0, "max_dd_r": 0.0, "gross_mean": 0.0,
                "cost_r": 0.0, "sd": 0.0, "reasons": {}, "by_symbol": {}, "by_direction": {}}
    observed, low, high = cluster_bootstrap(trades, confidence)
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value <= 0]
    loss_total = -sum(losses)
    deviation = stdev(values)
    equity = peak = drawdown = 0.0
    for trade in sorted(trades, key=lambda item: item["exit_date"]):
        equity += trade["r"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "name": name, "n": count, "clusters": clusters,
        "mean": observed, "low": low, "high": high,
        "win": len(wins) / count,
        "profit_factor": (sum(wins) / loss_total) if loss_total > 0 else None,
        "max_dd_r": drawdown,
        "gross_mean": mean([trade["gross_r"] for trade in trades]),
        "cost_r": mean([trade["cost_r"] for trade in trades]),
        "sd": deviation,
        "reasons": reason_histogram(trades),
        "by_symbol": per_symbol(trades),
        "by_direction": per_direction(trades),
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


def per_direction(trades: list[dict]) -> dict:
    out = {}
    for label, wanted in (("long", 1), ("short", -1)):
        values = [trade["r"] for trade in trades if trade["dir"] == wanted]
        out[label] = {"n": len(values), "mean": mean(values)}
    return out


# --------------------------------------------------------------------------- study


def pieces(candidate: str):
    if candidate == "c1_inside_breakout":
        bars = load("XAUUSD")
        return [("XAUUSD", bars, rule_c1(bars), simulate)]
    if candidate == "c2_ratio_reversion":
        gold, silver = load("XAUUSD"), load("XAGUSD")
        return [("XAUUSD", gold, rule_c2(gold, silver), simulate)]
    if candidate == "c3_fx_tsmom":
        out = []
        for symbol in ("EURUSD", "AUDUSD", "NZDUSD"):
            bars = load(symbol)
            out.append((symbol, bars, rule_c3(bars), simulate_monthly))
        return out
    raise SystemExit(f"unknown candidate {candidate}")


def _recorded_development(candidate: str) -> dict:
    """The development decision recorded on disk, and the numbers it claims."""
    if not DEV_EVIDENCE.exists():
        raise SystemExit(f"{DEV_EVIDENCE} is missing; run development first")
    report = json.loads(DEV_EVIDENCE.read_text(encoding="utf-8"))
    entry = report.get("candidates", {}).get(candidate, {})
    if report.get("window") != "dev" or not entry.get("decision", {}).get("passed"):
        raise SystemExit(f"{candidate} has no passed development run recorded; the holdout stays closed")
    return {"report": report, "entry": entry,
            "sha256": hashlib.sha256(DEV_EVIDENCE.read_bytes()).hexdigest()}


def require_holdout_open(candidate: str) -> dict:
    """A holdout is only scored if the lock exists, matches this engine and this evidence."""
    if not LOCKS.exists():
        raise SystemExit("no holdout lock; a holdout must be opened once, after development passes")
    entry = json.loads(LOCKS.read_text(encoding="utf-8")).get(candidate)
    if not entry:
        raise SystemExit(f"no holdout lock for {candidate}; development must pass first")
    recorded = _recorded_development(candidate)
    if entry.get("development_evidence_sha256") != recorded["sha256"]:
        raise SystemExit("the development evidence changed after the lock was written; refusing")
    if entry.get("engine_sha256") != engine_hash():
        raise SystemExit("the engine changed after the lock was written; the holdout would not be "
                         "the same rule, so it is refused")
    if entry.get("cache_manifest_sha256") != manifest_hash():
        raise SystemExit("the data cache changed after the lock was written; refusing")
    return entry


def open_holdout(candidate: str) -> str:
    """Open the holdout exactly once, after checking the recorded development result."""
    recorded = _recorded_development(candidate)
    decision = recorded["entry"]["decision"]
    # Recompute the development result and require the file to describe it. Fabricating a
    # passing document now also requires a passing run, because the numbers must agree.
    fresh = summarise(candidate, build(candidate, "dev"), DEV_CONFIDENCE)
    for field in ("n", "clusters", "mean", "low"):
        if abs(float(fresh[field]) - float(decision.get(field, 0.0))) > 1e-6:
            raise SystemExit(f"the recorded development {field} does not match a fresh run; refusing")
    digest = recorded["sha256"]
    locks = json.loads(LOCKS.read_text(encoding="utf-8")) if LOCKS.exists() else {}
    if candidate in locks:
        raise SystemExit(f"holdout already opened for {candidate} at {locks[candidate]['opened_at']}")
    locks[candidate] = {"opened_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "development_evidence_sha256": digest,
                        "engine_sha256": engine_hash(),
                        "cache_manifest_sha256": manifest_hash(),
                        "development_n": decision.get("n"), "development_low": decision.get("low")}
    LOCKS.parent.mkdir(parents=True, exist_ok=True)
    LOCKS.write_text(json.dumps(locks, indent=2), encoding="utf-8")
    return digest


def build(candidate: str, window: str, *, cost_multiple: float = 1.0,
          financing_multiple: float = 1.0) -> list[dict]:
    if window == "holdout":
        require_holdout_open(candidate)
    start, end = WINDOWS[window]
    trades: list[dict] = []
    for symbol, bars, rule, runner in pieces(candidate):
        trades.extend(runner(symbol, bars, rule, (start, end), cost_multiple=cost_multiple,
                             financing_multiple=financing_multiple))
    return trades


def cost_sweep(candidate: str, window: str) -> dict:
    """Re-price the same trades at 0x, transaction-only, base and double cost."""
    out = {}
    rows = (("zero", 0.0, 0.0), ("transaction_only", 1.0, 0.0),
            ("base", 1.0, 1.0), ("double", 2.0, 2.0))
    for label, transaction, financing in rows:
        values = [trade["r"] for trade in build(candidate, window, cost_multiple=transaction,
                                                financing_multiple=financing)]
        out[label] = {"n": len(values), "mean": mean(values)}
    return out


def verdict(summary: dict, window: str, *, double_cost_mean: float) -> dict:
    floor = MIN_DEV_TRADES if window == "dev" else MIN_HOLDOUT_TRADES
    confidence = DEV_CONFIDENCE if window == "dev" else HOLDOUT_CONFIDENCE
    if summary["n"] < floor:
        return {"status": "no_decision", "passed": False, "threshold_n": floor,
                "threshold_clusters": MIN_CLUSTERS, "confidence": confidence,
                "reason": f"n={summary['n']} is below the pre-registered floor of {floor}; no decision"}
    if summary["clusters"] < MIN_CLUSTERS:
        return {"status": "no_decision", "passed": False, "threshold_n": floor,
                "threshold_clusters": MIN_CLUSTERS, "confidence": confidence,
                "reason": f"only {summary['clusters']} clusters; the interval is not usable"}
    if summary["low"] > 0 and double_cost_mean > 0:
        status = "pass"
        reason = (f"mean net R {summary['mean']:+.4f}, {confidence:.2%} interval "
                  f"[{summary['low']:+.4f}, {summary['high']:+.4f}] entirely above zero")
        reason += f", and still {double_cost_mean:+.4f} R at double cost"
    elif summary["high"] < 0:
        status = "fail"
        reason = (f"mean net R {summary['mean']:+.4f}, {confidence:.2%} interval "
                  f"[{summary['low']:+.4f}, {summary['high']:+.4f}] entirely below zero")
    else:
        status = "fail"
        reason = (f"mean net R {summary['mean']:+.4f}, {confidence:.2%} interval "
                  f"[{summary['low']:+.4f}, {summary['high']:+.4f}] straddles zero")
        if double_cost_mean <= 0:
            reason += f"; the edge is also negative at double cost ({double_cost_mean:+.4f} R)"
    z_value = 2.394 if window == "dev" else 1.645
    detectable = z_value * summary.get("sd", 0.0) / math.sqrt(max(summary["clusters"], 1))
    return {"status": status, "passed": status == "pass", "threshold_n": floor,
            "threshold_clusters": MIN_CLUSTERS, "confidence": confidence,
            "detectable_r": detectable, "double_cost_mean": double_cost_mean, "reason": reason}


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
        open_holdout(wanted[0])

    confidence = DEV_CONFIDENCE if args.window == "dev" else HOLDOUT_CONFIDENCE
    report = {"window": args.window, "confidence": confidence, "seed": SEED,
              "bootstrap_rounds": BOOTSTRAP, "cache_manifest_sha256": manifest_hash(),
              "windows": {name: [low.isoformat(), high.isoformat()]
                          for name, (low, high) in WINDOWS.items()},
              "candidates": {}}
    print(f"window {args.window} ({WINDOWS[args.window][0]} .. {WINDOWS[args.window][1]}), "
          f"confidence {confidence:.4f}, {len(wanted)} candidate(s), "
          f"cache {manifest_hash()[:12]}")
    print(f"{'candidate':22}{'n':>6}{'clust':>6}{'win%':>7}{'meanR':>9}{'lowR':>9}{'highR':>9}{'costR':>8}")
    for candidate in wanted:
        trades = build(candidate, args.window)
        summary = summarise(candidate, trades, confidence)
        sweep = cost_sweep(candidate, args.window)
        double_cost = sweep["double"]["mean"]
        decision = verdict(summary, args.window, double_cost_mean=double_cost)
        summary["decision"] = decision
        summary["cost_sweep"] = sweep
        report["candidates"][candidate] = summary
        print(f"{candidate:22}{summary['n']:6}{summary['clusters']:6}{summary['win'] * 100:6.1f}%"
              f"{summary['mean']:+9.4f}{summary['low']:+9.4f}{summary['high']:+9.4f}"
              f"{summary['cost_r']:+8.4f}")
        print(f"    {decision['status'].upper()}: {decision['reason']}")
        print(f"    detectable edge at this confidence/cluster count: {decision['detectable_r']:.3f} R")
        print(f"    exits {summary['reasons']}  by symbol {summary['by_symbol']}  "
              f"by direction {summary['by_direction']}")
        if sweep:
            print(f"    cost sweep {sweep}")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"evidence: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
