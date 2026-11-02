#!/usr/bin/env python3
"""Paper-trade the same gold-style rule on three crypto venues at once.

Bybit, OKX and Kraken are polled for hourly candles; the rule is EMA 50/200 with a
2xATR stop and a 2R target, 0.5% risk of a fixed 10 000 bankroll per slot. Nothing is
sent anywhere: this is a demo run on real public prices, and its purpose is to compare
venues (spread, gaps, timing) rather than to promise a profit.
"""
from __future__ import annotations

import argparse
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "agent_data" / "multi_venue.jsonl"
RISK_FRACTION = 0.005
BANKROLL = 10_000.0


def _json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "dsh"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def candles_bybit(symbol: str) -> list[dict]:
    data = _json(f"https://api.bybit.com/v5/market/kline?category=spot&symbol={symbol}"
                 f"&interval=60&limit=500")["result"]["list"]
    rows = [{"t": int(item[0]), "o": float(item[1]), "h": float(item[2]),
             "l": float(item[3]), "c": float(item[4])} for item in data]
    return sorted(rows, key=lambda row: row["t"])


def candles_okx(inst: str) -> list[dict]:
    data = _json(f"https://www.okx.com/api/v5/market/candles?instId={inst}&bar=1H&limit=300")["data"]
    rows = [{"t": int(item[0]), "o": float(item[1]), "h": float(item[2]),
             "l": float(item[3]), "c": float(item[4])} for item in data]
    return sorted(rows, key=lambda row: row["t"])


def candles_kraken(pair: str) -> list[dict]:
    payload = _json(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=60")
    result = payload["result"]
    series = next(value for key, value in result.items() if key != "last")
    rows = [{"t": int(item[0]) * 1000, "o": float(item[1]), "h": float(item[2]),
             "l": float(item[3]), "c": float(item[4])} for item in series]
    return sorted(rows, key=lambda row: row["t"])


VENUES = {
    "bybit/BTCUSDT": (candles_bybit, "BTCUSDT"),
    "bybit/ETHUSDT": (candles_bybit, "ETHUSDT"),
    "okx/BTC-USDT": (candles_okx, "BTC-USDT"),
    "okx/ETH-USDT": (candles_okx, "ETH-USDT"),
    "kraken/XBTUSD": (candles_kraken, "XBTUSD"),
    "kraken/ETHUSD": (candles_kraken, "ETHUSD"),
}


def ema(values: list[float], period: int) -> float:
    weight = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = value * weight + result * (1.0 - weight)
    return result


def atr(rows: list[dict], period: int = 14) -> float:
    ranges = []
    for index in range(1, len(rows)):
        ranges.append(max(rows[index]["h"] - rows[index]["l"],
                          abs(rows[index]["h"] - rows[index - 1]["c"]),
                          abs(rows[index]["l"] - rows[index - 1]["c"])))
    window = ranges[-period:]
    return sum(window) / len(window)


def log_event(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(f"[{event['ts'][11:19]}] {event.get('venue', '')} {event['event']}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-sec", type=int, default=60)
    parser.add_argument("--minutes", type=int, default=0)
    args = parser.parse_args(argv)

    positions: dict[str, dict] = {}
    bankroll = BANKROLL
    deadline = time.monotonic() + args.minutes * 60 if args.minutes else None
    log_event({"event": f"MULTI-VENUE START: {len(VENUES)} slots, risk 0.5%, "
                        f"bankroll ${bankroll:.0f}"})

    while deadline is None or time.monotonic() < deadline:
        for name, (fetch, symbol) in VENUES.items():
            try:
                rows = fetch(symbol)
            except Exception as exc:  # noqa: BLE001 - a venue can refuse a call
                log_event({"venue": name, "event": f"feed error {type(exc).__name__}"})
                continue
            if len(rows) < 210:
                continue
            closes = [row["c"] for row in rows]
            price = closes[-1]
            fast, slow = ema(closes, 50), ema(closes, 200)
            position = positions.get(name)

            if position:
                sign = 1.0 if position["dir"] == "BUY" else -1.0
                pnl = (price - position["entry"]) * sign * position["units"]
                hit = None
                if sign > 0 and price <= position["sl"]:
                    hit = "SL"
                elif sign > 0 and price >= position["tp"]:
                    hit = "TP"
                elif sign < 0 and price >= position["sl"]:
                    hit = "SL"
                elif sign < 0 and price <= position["tp"]:
                    hit = "TP"
                if hit:
                    bankroll += pnl
                    log_event({"venue": name, "event": f"CLOSE {hit} {position['dir']} "
                                                       f"@ {price:.2f} P/L ${pnl:+.2f} | "
                                                       f"bankroll ${bankroll:.2f}"})
                    positions.pop(name, None)
                    continue

            if position is None:
                direction = "BUY" if fast > slow and price > slow else (
                    "SELL" if fast < slow and price < slow else None)
                if direction:
                    risk = bankroll * RISK_FRACTION
                    stop_distance = 2 * max(atr(rows), price * 0.001)
                    units = risk / stop_distance
                    entry = price
                    sl = entry - stop_distance if direction == "BUY" else entry + stop_distance
                    tp = entry + 2 * stop_distance if direction == "BUY" else entry - 2 * stop_distance
                    positions[name] = {"dir": direction, "entry": entry, "units": units,
                                       "sl": sl, "tp": tp}
                    log_event({"venue": name, "event": f"OPEN {direction} @ {entry:.2f} "
                                                       f"(SL {sl:.2f}, TP {tp:.2f}, "
                                                       f"risk ${risk:.2f})"})

        log_event({"event": f"tick: {len(positions)} open, bankroll ${bankroll:.2f}"})
        time.sleep(args.interval_sec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
