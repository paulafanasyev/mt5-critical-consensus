#!/usr/bin/env python3
"""Scan every MT5 symbol for an intraday window that pays.

A systematic search rather than a hunch: for each symbol, take every pair of entry and exit
hours in UTC, buy at the entry hour and sell at the exit hour, every trading day, and
report the mean net return after one round trip of spread. Sorted by t-statistic so the
strongest candidates surface first.

This is a search, not a proof: testing ~24x24 windows per symbol on 11 symbols means
thousands of hypotheses, so a top hit is a candidate for out-of-sample work, not a signal
to trade. The script says so in its own output.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone

SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD",
           "XAUUSD", "XAGUSD", "US500", "UK100")


def server_offset_seconds(tick_time: int, now: float) -> int:
    """How far ahead of UTC this server stamps its bars, measured rather than assumed.

    `rate["time"]` is server time, not UTC: on `MetaQuotes-Demo` a live tick whose real UTC
    moment was 07:52:48 came back as 09:52:46 (+7200 s), so the hour of a bar read straight off
    the epoch is the server's hour and the top window this scan prints as 00:00-01:00 is 22:00
    UTC. It also moves with the server's DST rule, so a schedule anchored to a UTC minute would
    silently drift three hours twice a year. Returned as whole hours because the sub-hour part
    is the age of the current tick, not a clock difference.
    """
    return int(round((tick_time - now) / 3600.0)) * 3600


def main() -> int:
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise SystemExit("MetaTrader5.initialize failed")
    results = []
    offsets = []
    try:
        for symbol in SYMBOLS:
            if not mt5.symbol_select(symbol, True):
                continue
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 20000)
            if rates is None or len(rates) < 2000:
                continue
            tick = mt5.symbol_info_tick(symbol)
            spread = ((tick.ask - tick.bid) / ((tick.ask + tick.bid) / 2)
                      if tick and tick.bid else 0.0)
            if tick and tick.time:
                offsets.append(server_offset_seconds(int(tick.time),
                                                     datetime.now(timezone.utc).timestamp()))
            # daily series of hourly closes, keyed by date and true UTC hour
            shift = offsets[-1] if offsets else 0
            by_day: dict[str, dict[int, float]] = {}
            for rate in rates:
                stamp = datetime.fromtimestamp(int(rate["time"]) - shift, tz=timezone.utc)
                by_day.setdefault(stamp.date().isoformat(), {})[stamp.hour] = float(rate["close"])
            days = sorted(by_day)
            for entry in range(0, 23):
                for exit_hour in range(entry + 1, min(entry + 12, 24)):
                    returns = []
                    for i in range(1, len(days)):
                        today = by_day[days[i]]
                        if entry not in today or exit_hour not in today:
                            continue
                        start = today[entry]
                        end = today[exit_hour]
                        if start and end:
                            returns.append(end / start - 1 - spread)
                    if len(returns) < 200:
                        continue
                    mean = statistics.fmean(returns)
                    sd = statistics.pstdev(returns) / math.sqrt(len(returns))
                    if sd == 0:
                        continue
                    results.append({"symbol": symbol, "entry": entry, "exit": exit_hour,
                                    "n": len(returns), "mean_pct": mean * 100,
                                    "t": mean / sd, "annual_pct": mean * 252 * 100})
    finally:
        mt5.shutdown()

    results.sort(key=lambda row: row["t"], reverse=True)
    shift = max(set(offsets), key=offsets.count) if offsets else 0
    print("top 15 intraday windows by t-statistic (costs included, one round trip per day)")
    print(f"hours are true UTC; this server was {shift // 3600} h ahead of UTC at the read")
    print(f"{'symbol':8} {'window':>11} {'n':>6} {'mean%':>9} {'t':>6} {'annual%':>9}")
    for row in results[:15]:
        print(f"{row['symbol']:8} {row['entry']:02d}:00-{row['exit']:02d}:00 {row['n']:6} "
              f"{row['mean_pct']:>+9.4f} {row['t']:>6.2f} {row['annual_pct']:>+9.1f}")
    print("\nworst 5:")
    for row in results[-5:]:
        print(f"{row['symbol']:8} {row['entry']:02d}:00-{row['exit']:02d}:00 {row['n']:6} "
              f"{row['mean_pct']:>+9.4f} {row['t']:>6.2f} {row['annual_pct']:>+9.1f}")
    strong = [row for row in results if abs(row["t"]) > 3]
    print(f"\nwindows with |t| > 3: {len(strong)} of {len(results)} tested "
          f"(expected by chance at 5%: about {int(len(results) * 0.05)})")
    print("A candidate needs out-of-sample confirmation before any money; this table is a "
          "search over thousands of hypotheses, not a discovery.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
