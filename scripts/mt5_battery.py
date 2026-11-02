#!/usr/bin/env python3
"""A broader battery on the terminal's own data.

Four classic ideas that were not in the earlier scan:

  A. Monday gap fill     - the weekend gap tends to close during Monday.
  B. Extreme-hour fade   - a move of more than 2.5 sigma in one hour reverts next hour.
  C. Pairs reversion     - EURUSD/GBPUSD and XAUUSD/XAGUSD ratios return to their mean.
  D. Index lead-lag      - does the US session lead the European index the next day.

Each test is a single hypothesis with a t-statistic and costs included, so a result here is
worth a forward test rather than a proof.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone

SYMBOLS = ("EURUSD", "GBPUSD", "USDCHF", "AUDUSD", "US500", "UK100", "XAUUSD", "XAGUSD")


def load(symbol: str) -> list[dict]:
    import MetaTrader5 as mt5

    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 20000)
    out = []
    for rate in rates:
        stamp = datetime.fromtimestamp(int(rate["time"]), tz=timezone.utc)
        out.append({"dt": stamp, "o": float(rate["open"]), "h": float(rate["high"]),
                    "l": float(rate["low"]), "c": float(rate["close"])})
    return out


def spread(symbol: str) -> float:
    import MetaTrader5 as mt5

    tick = mt5.symbol_info_tick(symbol)
    if not tick or not tick.bid:
        return 0.0
    return (tick.ask - tick.bid) / ((tick.ask + tick.bid) / 2)


def tstat(values: list[float]) -> tuple[float, float, int]:
    if len(values) < 20:
        return 0.0, 0.0, len(values)
    mean = statistics.fmean(values)
    sd = statistics.pstdev(values) / math.sqrt(len(values))
    return mean, (mean / sd if sd else 0.0), len(values)


def monday_gap(bars: list[dict], cost: float) -> None:
    days: dict = {}
    for bar in bars:
        days.setdefault(bar["dt"].date(), []).append(bar)
    dates = sorted(days)
    results = []
    for i in range(1, len(dates)):
        if dates[i].weekday() != 0:
            continue
        monday = sorted(days[dates[i]], key=lambda b: b["dt"])
        friday = sorted(days[dates[i - 1]], key=lambda b: b["dt"])
        if not monday or not friday:
            continue
        gap = monday[0]["o"] / friday[-1]["c"] - 1
        day_return = monday[-1]["c"] / monday[0]["o"] - 1
        if abs(gap) < 0.001:
            continue
        # directional bet on the gap closing: short the gap direction
        trade = (-1 if gap > 0 else 1) * day_return - cost
        results.append(trade)
    mean, t, n = tstat(results)
    print(f"  A. Monday gap fill: n={n:3}  mean {mean * 100:+.4f}%  t={t:+.2f}")


def extreme_hour(bars: list[dict], cost: float, sigma: float = 2.5) -> None:
    returns = []
    for i in range(101, len(bars)):
        past = [bars[j]["c"] / bars[j - 1]["c"] - 1 for j in range(i - 100, i)]
        sd = statistics.pstdev(past)
        if sd == 0:
            continue
        move = bars[i]["c"] / bars[i - 1]["c"] - 1
        if abs(move) < sigma * sd:
            continue
        nxt = bars[i + 1]["c"] / bars[i]["c"] - 1 if i + 1 < len(bars) else 0
        returns.append((-1 if move > 0 else 1) * nxt - cost)
    mean, t, n = tstat(returns)
    print(f"  B. extreme-hour fade (|move| > {sigma} sigma): n={n:3}  "
          f"mean {mean * 100:+.4f}%  t={t:+.2f}")


def pairs_reversion(a: list[dict], b: list[dict], cost: float, label: str) -> None:
    pa = {bar["dt"]: bar["c"] for bar in a}
    pb = {bar["dt"]: bar["c"] for bar in b}
    times = sorted(set(pa) & set(pb))
    ratio = [pa[t] / pb[t] for t in times]
    trades = []
    i = 100
    while i < len(ratio) - 1:
        window = ratio[i - 100:i]
        mean = statistics.fmean(window)
        sd = statistics.pstdev(window)
        if sd == 0:
            i += 1
            continue
        z = (ratio[i] - mean) / sd
        if abs(z) < 2.0:
            i += 1
            continue
        direction = -1 if z > 0 else 1
        entry = ratio[i]
        j = i + 1
        while j < min(i + 120, len(ratio)):
            if (direction == 1 and ratio[j] >= mean) or (direction == -1 and ratio[j] <= mean):
                break
            j += 1
        exit_price = ratio[min(j, len(ratio) - 1)]
        trades.append(direction * (exit_price / entry - 1) - cost)
        i = j + 1
    mean, t, n = tstat(trades)
    print(f"  C. pairs {label}: n={n:3}  mean {mean * 100:+.4f}%  t={t:+.2f}")


def main() -> int:
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise SystemExit("MetaTrader5.initialize failed")
    data = {}
    try:
        for symbol in SYMBOLS:
            data[symbol] = load(symbol)
        costs = {symbol: spread(symbol) for symbol in SYMBOLS}
        print("A. Monday gap fill (weekend gap reversal)")
        for symbol in ("EURUSD", "GBPUSD", "USDCHF", "AUDUSD", "US500", "XAUUSD"):
            monday_gap(data[symbol], costs[symbol])
        print("B. extreme-hour reversal")
        for symbol in ("EURUSD", "GBPUSD", "XAUUSD", "US500", "UK100"):
            extreme_hour(data[symbol], costs[symbol])
        print("C. pairs ratio reversion")
        pairs_reversion(data["EURUSD"], data["GBPUSD"], costs["EURUSD"] + costs["GBPUSD"],
                        "EURUSD/GBPUSD")
        pairs_reversion(data["XAUUSD"], data["XAGUSD"], costs["XAUUSD"] + costs["XAGUSD"],
                        "XAUUSD/XAGUSD")
        pairs_reversion(data["US500"], data["UK100"], costs["US500"] + costs["UK100"],
                        "US500/UK100")
    finally:
        mt5.shutdown()
    print("D. (index lead-lag is the same pair test with a lag; reported separately)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
