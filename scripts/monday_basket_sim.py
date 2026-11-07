#!/usr/bin/env python3
"""What the Monday basket pays, in dollars, week by week.

Replays every Monday in the terminal's history with the exact sizes the demo uses, so the
first live Monday can be compared against a distribution instead of a guess:

  - Monday long:  0.10 lots on each of GBPUSD, AUDUSD, USDCHF, EURUSD, USDJPY
  - Monday gap:   0.02 lots on each of the four gap pairs, 0.5% stop, only when |gap|>0.1%
  - US500:        1 lot, Monday session only

Outputs the mean, standard deviation, the ten worst and ten best weeks, and the equity
curve on a 100 000 account.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone

LONG_PAIRS = ("GBPUSD", "AUDUSD", "USDCHF", "EURUSD", "USDJPY")
GAP_PAIRS = ("GBPUSD", "USDCHF", "AUDUSD", "USDCAD")
LONG_LOTS = 0.10
GAP_LOTS = 0.02
US500_LOTS = 1.0
ACCOUNT = 100_000.0
CONTRACT = 100_000.0   # one lot of a currency pair
MIN_GAP = 0.001
GAP_STOP = 0.005


def load(symbol: str) -> tuple[dict, float]:
    import MetaTrader5 as mt5

    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 20000)
    tick = mt5.symbol_info_tick(symbol)
    cost = ((tick.ask - tick.bid) / ((tick.ask + tick.bid) / 2)) if tick and tick.bid else 0.0
    days: dict = {}
    for rate in rates:
        stamp = datetime.fromtimestamp(int(rate["time"]), tz=timezone.utc)
        days.setdefault(stamp.date(), []).append(
            {"dt": stamp, "o": float(rate["open"]), "h": float(rate["high"]),
             "l": float(rate["low"]), "c": float(rate["close"])})
    return days, cost


def main() -> int:
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise SystemExit("MetaTrader5.initialize failed")
    weekly: dict = {}
    try:
        data = {}
        for symbol in set(LONG_PAIRS) | set(GAP_PAIRS) | {"US500"}:
            data[symbol] = load(symbol)
        for symbol in LONG_PAIRS:
            days, cost = data[symbol]
            dates = sorted(days)
            for i in range(len(dates)):
                if dates[i].weekday() != 0:
                    continue
                bars = sorted(days[dates[i]], key=lambda bar: bar["dt"])
                if not bars:
                    continue
                move = bars[-1]["c"] / bars[0]["o"] - 1 - cost
                weekly[dates[i]] = weekly.get(dates[i], 0.0) + move * LONG_LOTS * CONTRACT

        for symbol in GAP_PAIRS:
            days, cost = data[symbol]
            dates = sorted(days)
            for i in range(1, len(dates)):
                if dates[i].weekday() != 0:
                    continue
                monday = sorted(days[dates[i]], key=lambda bar: bar["dt"])
                friday = sorted(days[dates[i - 1]], key=lambda bar: bar["dt"])
                if not monday or not friday:
                    continue
                gap = monday[0]["o"] / friday[-1]["c"] - 1
                if abs(gap) < MIN_GAP:
                    continue
                direction = -1 if gap > 0 else 1
                entry = monday[0]["o"]
                stop = entry * (1 - direction * GAP_STOP)
                result = None
                for bar in monday:
                    if direction == 1 and bar["l"] <= stop:
                        result = -GAP_STOP - cost
                        break
                    if direction == -1 and bar["h"] >= stop:
                        result = -GAP_STOP - cost
                        break
                if result is None:
                    result = direction * (monday[-1]["c"] / entry - 1) - cost
                weekly[dates[i]] = weekly.get(dates[i], 0.0) + result * GAP_LOTS * CONTRACT

        days, cost = data["US500"]
        dates = sorted(days)
        for i in range(len(dates)):
            if dates[i].weekday() != 0:
                continue
            bars = sorted(days[dates[i]], key=lambda bar: bar["dt"])
            if not bars:
                continue
            move = bars[-1]["c"] / bars[0]["o"] - 1 - cost
            weekly[dates[i]] = weekly.get(dates[i], 0.0) + move * US500_LOTS * 7600
    finally:
        mt5.shutdown()

    dates = sorted(weekly)
    values = [weekly[date] for date in dates]
    mean = statistics.fmean(values)
    sd = statistics.pstdev(values)
    equity = ACCOUNT
    peak = equity
    drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    print(f"weeks {len(values)}  {dates[0]} .. {dates[-1]}")
    print(f"mean per week ${mean:+.2f}  median ${statistics.median(values):+.2f}  sd ${sd:.2f}")
    print(f"annualised on ${ACCOUNT:.0f}: {mean * 52 / ACCOUNT * 100:+.1f}%")
    print(f"worst weeks: {[round(v, 1) for v in sorted(values)[:10]]}")
    print(f"best weeks:  {[round(v, 1) for v in sorted(values)[-10:]]}")
    print(f"final equity ${equity:.2f}  max drawdown ${drawdown:.2f} "
          f"({drawdown / ACCOUNT * 100:.1f}%)")
    wins = sum(1 for value in values if value > 0) / len(values)
    print(f"positive weeks {wins * 100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
