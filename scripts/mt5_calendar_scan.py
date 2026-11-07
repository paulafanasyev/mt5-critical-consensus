#!/usr/bin/env python3
"""Calendar and session effects around the month, the week and the London open.

The Monday gap was found with a single scan. This script widens the net and, importantly,
checks whether the winner survives a stop-loss, because a gap trade that never uses a stop
is not yet a strategy:

  A. day-of-week drift per symbol, five separate hypotheses;
  B. turn-of-month, the last day of a month plus the first two;
  C. Monday gap fill with a hard stop, to see if the edge is robust to being wrong;
  D. Asian-range fade at the London open.

Each row is one hypothesis with a t-statistic and the spread included.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone

SYMBOLS = ("EURUSD", "GBPUSD", "USDCHF", "AUDUSD", "USDCAD", "USDJPY",
           "US500", "UK100", "XAUUSD", "XAGUSD")


def stats(values: list[float]) -> tuple[float, float, int]:
    if len(values) < 20:
        return 0.0, 0.0, len(values)
    mean = statistics.fmean(values)
    sd = statistics.pstdev(values) / math.sqrt(len(values))
    return mean, (mean / sd if sd else 0.0), len(values)


def load(symbol: str) -> tuple[list[dict], float]:
    import MetaTrader5 as mt5

    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 20000)
    tick = mt5.symbol_info_tick(symbol)
    cost = ((tick.ask - tick.bid) / ((tick.ask + tick.bid) / 2)) if tick and tick.bid else 0.0
    bars = [{"dt": datetime.fromtimestamp(int(rate["time"]), tz=timezone.utc),
             "o": float(rate["open"]), "h": float(rate["high"]),
             "l": float(rate["low"]), "c": float(rate["close"])} for rate in rates]
    return bars, cost


def by_day(bars: list[dict]) -> dict:
    days: dict = {}
    for bar in bars:
        days.setdefault(bar["dt"].date(), []).append(bar)
    return days


def day_of_week(days: dict, cost: float, symbol: str) -> None:
    dates = sorted(days)
    buckets: dict[int, list[float]] = {i: [] for i in range(7)}
    for i in range(1, len(dates)):
        previous = sorted(days[dates[i - 1]], key=lambda bar: bar["dt"])
        current = sorted(days[dates[i]], key=lambda bar: bar["dt"])
        if not previous or not current:
            continue
        value = current[-1]["c"] / previous[-1]["c"] - 1 - cost
        buckets[current[0]["dt"].weekday()].append(value)
    names = ["Пн", "Вт", "Ср", "Чт", "Пт"]
    parts = []
    for weekday in range(5):
        mean, t, n = stats(buckets[weekday])
        parts.append(f"{names[weekday]} {mean * 100:+.3f}% (t={t:+.2f}, n={n})")
    print(f"  {symbol:7} " + "  ".join(parts))


def turn_of_month(days: dict, cost: float, symbol: str) -> None:
    dates = sorted(days)
    trades = []
    for i in range(1, len(dates) - 2):
        current_month = dates[i].month
        previous_month = dates[i - 1].month
        if current_month == previous_month:
            continue
        entry = sorted(days[dates[i - 1]], key=lambda bar: bar["dt"])[-1]["c"]
        exit_price = sorted(days[dates[i + 1]], key=lambda bar: bar["dt"])[-1]["c"]
        trades.append(exit_price / entry - 1 - cost)
    mean, t, n = stats(trades)
    print(f"  {symbol:7} turn-of-month: {mean * 100:+.3f}%  t={t:+.2f}  n={n}")


def monday_gap_stop(days: dict, cost: float, symbol: str, stop: float = 0.005) -> None:
    dates = sorted(days)
    out = []
    for i in range(1, len(dates)):
        if dates[i].weekday() != 0:
            continue
        monday = sorted(days[dates[i]], key=lambda bar: bar["dt"])
        friday = sorted(days[dates[i - 1]], key=lambda bar: bar["dt"])
        if not monday or not friday:
            continue
        gap = monday[0]["o"] / friday[-1]["c"] - 1
        if abs(gap) < 0.001:
            continue
        direction = -1 if gap > 0 else 1
        entry = monday[0]["o"]
        stop_price = entry * (1 - direction * stop)
        result = None
        for bar in monday:
            if direction == 1 and bar["l"] <= stop_price:
                result = -stop - cost
                break
            if direction == -1 and bar["h"] >= stop_price:
                result = -stop - cost
                break
        if result is None:
            result = direction * (monday[-1]["c"] / entry - 1) - cost
        out.append(result)
    mean, t, n = stats(out)
    print(f"  {symbol:7} Monday gap + {stop * 100:.1f}% stop: {mean * 100:+.3f}%  t={t:+.2f}  n={n}")


def asian_range_fade(days: dict, cost: float, symbol: str) -> None:
    """Fade a break of the Asian range during the London session."""
    trades = []
    for date, bars in days.items():
        ordered = sorted(bars, key=lambda bar: bar["dt"])
        asian = [bar for bar in ordered if 0 <= bar["dt"].hour < 6]
        london = [bar for bar in ordered if 7 <= bar["dt"].hour < 16]
        if len(asian) < 4 or len(london) < 4:
            continue
        high = max(bar["h"] for bar in asian)
        low = min(bar["l"] for bar in asian)
        if high <= low:
            continue
        first = london[0]
        direction = 0
        if first["o"] > high:
            direction = -1
        elif first["o"] < low:
            direction = 1
        if direction == 0:
            continue
        entry = first["o"]
        exit_price = london[-1]["c"]
        trades.append(direction * (exit_price / entry - 1) - cost)
    mean, t, n = stats(trades)
    print(f"  {symbol:7} Asian-range fade: {mean * 100:+.3f}%  t={t:+.2f}  n={n}")


def main() -> int:
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise SystemExit("MetaTrader5.initialize failed")
    data = {}
    try:
        for symbol in SYMBOLS:
            data[symbol] = load(symbol)
    finally:
        mt5.shutdown()

    print("A. день недели (дневная доходность, спред вычтен)")
    for symbol in SYMBOLS:
        day_of_week(by_day(data[symbol][0]), data[symbol][1], symbol)
    print("\nB. конец месяца")
    for symbol in SYMBOLS:
        turn_of_month(by_day(data[symbol][0]), data[symbol][1], symbol)
    print("\nC. гэп понедельника со стопом 0.5%")
    for symbol in SYMBOLS:
        monday_gap_stop(by_day(data[symbol][0]), data[symbol][1], symbol)
    print("\nD. фейд азиатского диапазона на открытии Лондона")
    for symbol in SYMBOLS:
        asian_range_fade(by_day(data[symbol][0]), data[symbol][1], symbol)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
