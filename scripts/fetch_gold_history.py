#!/usr/bin/env python3
"""Download the full gold history that is actually available, from two venues.

- Binance PAXG/USDT (tokenised gold): spot daily and hourly, from the first listed bar.
- MetaTrader 5 XAUUSD: whatever the broker terminal serves, hourly and daily.

Saved under agent_data/gold_data/ (gitignored) as JSON: [{t, o, h, l, c, v}, ...] with
t in milliseconds UTC. Nothing here trades; it is data collection for a pre-registered
study, and the study may conclude that no rule survives.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "agent_data" / "gold_data"
BINANCE = "https://api.binance.com/api/v3/klines"


def _get(params: dict) -> list:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(f"{BINANCE}?{query}", headers={"User-Agent": "dsh"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def binance(symbol: str, interval: str, *, pause: float = 0.25) -> list[dict]:
    rows: list[dict] = []
    start = 0
    while True:
        batch = _get({"symbol": symbol, "interval": interval, "limit": 1000,
                      "startTime": start})
        if not batch:
            break
        rows.extend({"t": int(item[0]), "o": float(item[1]), "h": float(item[2]),
                     "l": float(item[3]), "c": float(item[4]), "v": float(item[5])}
                    for item in batch)
        start = int(batch[-1][0]) + 1
        if len(batch) < 1000:
            break
        time.sleep(pause)
    return rows


def mt5_history(symbol: str, timeframe: str, bars: int) -> list[dict]:
    import MetaTrader5 as mt5

    frames = {"H1": mt5.TIMEFRAME_H1, "D1": mt5.TIMEFRAME_D1, "H4": mt5.TIMEFRAME_H4}
    if not mt5.initialize():
        raise SystemExit("MetaTrader5.initialize failed")
    try:
        mt5.symbol_select(symbol, True)
        rates = mt5.copy_rates_from_pos(symbol, frames[timeframe], 0, bars)
    finally:
        mt5.shutdown()
    if rates is None:
        return []
    return [{"t": int(rate["time"]) * 1000, "o": float(rate["open"]), "h": float(rate["high"]),
             "l": float(rate["low"]), "c": float(rate["close"]),
             "v": float(rate["tick_volume"])} for rate in rates]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-binance", action="store_true")
    parser.add_argument("--skip-mt5", action="store_true")
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)

    if not args.skip_binance:
        for interval, name in (("1d", "paxg_d1"), ("1h", "paxg_h1")):
            rows = binance("PAXGUSDT", interval)
            path = OUT / f"{name}.json"
            path.write_text(json.dumps(rows), encoding="utf-8")
            first = datetime.fromtimestamp(rows[0]["t"] / 1000, tz=timezone.utc)
            last = datetime.fromtimestamp(rows[-1]["t"] / 1000, tz=timezone.utc)
            print(f"{name}: {len(rows)} bars {first:%Y-%m-%d} .. {last:%Y-%m-%d} -> {path}")

    if not args.skip_mt5:
        for timeframe, name in (("H1", "xauusd_h1"), ("D1", "xauusd_d1")):
            rows = mt5_history("XAUUSD", timeframe, 20_000)
            if not rows:
                print(f"{name}: the terminal returned no bars")
                continue
            path = OUT / f"{name}.json"
            path.write_text(json.dumps(rows), encoding="utf-8")
            first = datetime.fromtimestamp(rows[0]["t"] / 1000, tz=timezone.utc)
            last = datetime.fromtimestamp(rows[-1]["t"] / 1000, tz=timezone.utc)
            print(f"{name}: {len(rows)} bars {first:%Y-%m-%d} .. {last:%Y-%m-%d} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
