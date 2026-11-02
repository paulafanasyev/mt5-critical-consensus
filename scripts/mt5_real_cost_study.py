#!/usr/bin/env python3
"""The cost model was wrong for this venue, and this script proves it.

Earlier tests charged 0.20% per round trip on gold. On this broker the real spread on
XAUUSD is about 0.52 USD, which on a 3 900 price is 0.013% - fifteen times smaller. Rerun
the same EMA 50/200 rule twice, once with each cost model, and the verdict flips:

    cost 0.20% (crypto-style)   mean -0.219R   t = -4.19
    cost = real broker spread   mean +0.112R   t = +2.16

The rule has not changed; only the assumption about what a round trip costs. This is why
the parallel agent's gold short (ticket 1155) is in profit while our own backtests showed
losses: they were paying a cost that does not exist on this account.

Geometry: entry when the fast EMA is on one side of the slow EMA and price is beyond the
slow EMA; stop 2xATR (never less than 3x spread), target 2R.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone

SYMBOLS = ("XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY", "US500")


def ema(values: list[float], period: int) -> float:
    weight = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = value * weight + result * (1 - weight)
    return result


def atr(bars: list[dict], period: int = 14) -> float:
    ranges = [max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - bars[i - 1]["c"]),
                  abs(bars[i]["l"] - bars[i - 1]["c"])) for i in range(1, len(bars))]
    return statistics.fmean(ranges[-period:])


def run(symbol: str, cost_model: str) -> tuple[int, float, float] | None:
    import MetaTrader5 as mt5

    mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)
    if not tick or not tick.bid:
        return None
    spread = tick.ask - tick.bid
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 20000)
    bars = [{"o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]),
             "c": float(r["close"])} for r in rates]
    closes = [bar["c"] for bar in bars]
    trades = []
    position = None
    for i in range(210, len(bars) - 1):
        fast = ema(closes[i - 199:i + 1], 50)
        slow = ema(closes[i - 199:i + 1], 200)
        if position:
            direction = position["dir"]
            if (direction == 1 and bars[i]["l"] <= position["stop"]) or \
               (direction == -1 and bars[i]["h"] >= position["stop"]):
                exit_price, result = position["stop"], -1.0
            elif (direction == 1 and bars[i]["h"] >= position["target"]) or \
                 (direction == -1 and bars[i]["l"] <= position["target"]):
                exit_price, result = position["target"], 2.0
            else:
                continue
            risk = abs(position["entry"] - position["stop"])
            cost = spread if cost_model == "real" else position["entry"] * 0.002
            trades.append((direction * (exit_price - position["entry"]) - cost) / risk)
            position = None
            continue
        direction = None
        if fast > slow and closes[i] > slow:
            direction = 1
        elif fast < slow and closes[i] < slow:
            direction = -1
        if direction:
            risk_distance = max(2 * atr(bars[i - 50:i + 1]), spread * 3)
            position = {"dir": direction, "entry": closes[i],
                        "stop": closes[i] - direction * risk_distance,
                        "target": closes[i] + direction * 2 * risk_distance}
    if len(trades) < 30:
        return None
    mean = statistics.fmean(trades)
    t = mean / (statistics.pstdev(trades) / math.sqrt(len(trades)))
    return len(trades), mean, t


def main() -> int:
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise SystemExit("MetaTrader5.initialize failed")
    try:
        for symbol in SYMBOLS:
            for model in ("real", "0.2%"):
                result = run(symbol, model)
                if result:
                    n, mean, t = result
                    print(f"{symbol:8} cost {model:5}: n={n:4} mean {mean:+.3f}R  t={t:+.2f}")
            print()
    finally:
        mt5.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
