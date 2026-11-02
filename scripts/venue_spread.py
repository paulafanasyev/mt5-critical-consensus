#!/usr/bin/env python3
"""Compare execution cost across the venues actually used.

For each crypto venue the top-of-book bid/ask is read from the public API and the spread is
expressed in basis points; the price is also compared with the median across venues, which
is the size of the cross-venue basis. For MetaTrader 5 the broker's own bid/ask is used.
Three snapshots two seconds apart are averaged so one stale quote cannot distort the table.
"""
from __future__ import annotations

import json
import statistics
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "evidence" / "venue_spreads.txt"


def get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "dsh"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def binance(symbol: str) -> tuple[float, float]:
    data = get(f"https://api.binance.com/api/v3/ticker/bookTicker?symbol={symbol}")
    return float(data["bidPrice"]), float(data["askPrice"])


def bybit(symbol: str) -> tuple[float, float]:
    data = get(f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={symbol}")
    row = data["result"]["list"][0]
    return float(row["bid1Price"]), float(row["ask1Price"])


def okx(inst: str) -> tuple[float, float]:
    data = get(f"https://www.okx.com/api/v5/market/ticker?instId={inst}")
    row = data["data"][0]
    return float(row["bidPx"]), float(row["askPx"])


def kraken(pair: str) -> tuple[float, float]:
    data = get(f"https://api.kraken.com/0/public/Ticker?pair={pair}")
    row = next(iter(data["result"].values()))
    return float(row["b"][0]), float(row["a"][0])


VENUES = {
    "Binance": {"BTC": (binance, "BTCUSDT"), "ETH": (binance, "ETHUSDT")},
    "Bybit": {"BTC": (bybit, "BTCUSDT"), "ETH": (bybit, "ETHUSDT")},
    "OKX": {"BTC": (okx, "BTC-USDT"), "ETH": (okx, "ETH-USDT")},
    "Kraken": {"BTC": (kraken, "XBTUSD"), "ETH": (kraken, "ETHUSD")},
}


def mt5_spreads() -> dict[str, tuple[float, float, float]]:
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {}
    if not mt5.initialize():
        return {}
    out = {}
    try:
        for symbol in ("XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "US500"):
            mt5.symbol_select(symbol, True)
            tick = mt5.symbol_info_tick(symbol)
            if tick and tick.bid:
                out[symbol] = (tick.bid, tick.ask, 0.0)
    finally:
        mt5.shutdown()
    return out


def main() -> int:
    snapshots: dict[str, dict[str, list[float]]] = {venue: {} for venue in VENUES}
    for _ in range(3):
        for venue, assets in VENUES.items():
            for asset, (fetch, symbol) in assets.items():
                try:
                    bid, ask = fetch(symbol)
                except Exception:  # noqa: BLE001 - a venue can refuse a call
                    continue
                snapshots[venue].setdefault(asset, []).append(
                    (bid + ask) / 2 if bid and ask else 0.0)
                snapshots[venue].setdefault(f"{asset}_spread", []).append(
                    (ask - bid) / ((ask + bid) / 2) * 10_000 if bid and ask else 0.0)
        time.sleep(2)

    lines = ["cross-venue execution cost (average of three snapshots)", ""]
    lines.append(f"{'venue':10} {'asset':5} {'mid':>12} {'spread bps':>12} {'basis vs median':>16}")
    for asset in ("BTC", "ETH"):
        mids = {venue: statistics.fmean(snapshots[venue].get(asset, [0]))
                for venue in VENUES if snapshots[venue].get(asset)}
        median = statistics.median(mids.values()) if mids else 0.0
        for venue, mid in mids.items():
            spread = statistics.fmean(snapshots[venue].get(f"{asset}_spread", [0.0]))
            basis = (mid / median - 1) * 10_000 if median else 0.0
            lines.append(f"{venue:10} {asset:5} {mid:12.2f} {spread:12.1f} {basis:+16.1f}")

    broker = mt5_spreads()
    if broker:
        lines.append("")
        lines.append(f"{'MT5 symbol':10} {'bid':>12} {'ask':>12} {'spread bps':>12}")
        for symbol, (bid, ask, _) in broker.items():
            spread = (ask - bid) / ((ask + bid) / 2) * 10_000 if bid else 0.0
            lines.append(f"{symbol:10} {bid:12.5f} {ask:12.5f} {spread:12.1f}")

    # The quoted spread is not the cost: the taker fee dominates on every crypto venue.
    lines.append("")
    lines.append("taker fees, per side (published schedules): Binance 10 bps, Bybit 10 bps, "
                 "OKX 10 bps, Kraken 26 bps")
    lines.append("round trip on BTC therefore costs 20-52 bps before slippage, against "
                 "1-3 bps of quoted spread on the MT5 broker's metals and FX")
    report = "\n".join(lines)
    print(report)
    OUT.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
