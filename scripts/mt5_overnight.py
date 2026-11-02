#!/usr/bin/env python3
"""Measure the overnight drift on the broker's own instruments.

Before risking a demo order, check whether the anomaly found in the index data exists on
this venue at all: group the terminal's hourly bars into server days, compare each day's
first open with the previous day's last close, and report the mean and t-statistic against
the broker's real spread.

XAUUSD is a spot CFD: if its overnight drift is the tiny spot figure rather than the
futures figure, the trade does not survive this venue's costs.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone

SYMBOLS = ("XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "US500", "USDJPY", "AUDUSD", "USDCHF")


def fetch(symbol: str, bars: int = 20000):
    import MetaTrader5 as mt5

    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, bars)
    return rates


def main() -> int:
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise SystemExit("MetaTrader5.initialize failed")
    rows = []
    try:
        for symbol in SYMBOLS:
            try:
                rates = fetch(symbol)
            except Exception as exc:  # noqa: BLE001
                print(f"{symbol}: {type(exc).__name__}")
                continue
            if rates is None or len(rates) < 500:
                print(f"{symbol}: not enough bars")
                continue
            days: dict[int, list[dict]] = {}
            for rate in rates:
                server_day = int(rate["time"]) // 86400
                days.setdefault(server_day, []).append(
                    {"o": float(rate["open"]), "c": float(rate["close"]),
                     "t": int(rate["time"])})
            keys = sorted(days)
            overnight, intraday = [], []
            for i in range(1, len(keys)):
                previous = sorted(days[keys[i - 1]], key=lambda item: item["t"])
                current = sorted(days[keys[i]], key=lambda item: item["t"])
                if not previous or not current:
                    continue
                overnight.append(current[0]["o"] / previous[-1]["c"] - 1)
                intraday.append(current[-1]["c"] / current[0]["o"] - 1)
            if len(overnight) < 200:
                continue
            o_mean = statistics.fmean(overnight)
            o_t = o_mean / (statistics.pstdev(overnight) / math.sqrt(len(overnight)))
            i_mean = statistics.fmean(intraday)
            i_t = i_mean / (statistics.pstdev(intraday) / math.sqrt(len(intraday)))
            info = mt5.symbol_info(symbol)
            tick = mt5.symbol_info_tick(symbol)
            spread_bps = ((tick.ask - tick.bid) / ((tick.ask + tick.bid) / 2) * 10_000
                          if tick and tick.bid else None)
            rows.append({"symbol": symbol, "n": len(overnight),
                         "overnight_pct": o_mean * 100, "o_t": o_t,
                         "intraday_pct": i_mean * 100, "i_t": i_t,
                         "spread_bps": spread_bps,
                         "net_long_pct": (o_mean - (spread_bps or 0) / 10_000) * 100,
                         "net_short_pct": (-o_mean - (spread_bps or 0) / 10_000) * 100})
    finally:
        mt5.shutdown()

    rows.sort(key=lambda row: row["overnight_pct"])
    print(f"{'symbol':8} {'days':>6} {'overnight%':>11} {'t':>6} {'intraday%':>10} {'t':>6} "
          f"{'spread bps':>10} {'long net%/day':>14} {'short net%/day':>15}")
    for row in rows:
        print(f"{row['symbol']:8} {row['n']:6} {row['overnight_pct']:>+11.5f} {row['o_t']:>6.2f} "
              f"{row['intraday_pct']:>+10.5f} {row['i_t']:>6.2f} {row['spread_bps'] or 0:>10.1f} "
              f"{row['net_long_pct']:>+14.5f} {row['net_short_pct']:>+15.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
