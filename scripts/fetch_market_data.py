#!/usr/bin/env python3
"""Download the data the strategy library says we are missing.

Yahoo Finance's chart endpoint is free and needs no key; it returns daily history for
indices, yields, the dollar index, ETFs, futures and crypto. Deribit's public API gives
the crypto volatility index (DVOL). FRED and Stooq were tried first and are blocked from
this host.

Saved under agent_data/market_data/<name>.json as {symbol, name, bars: [{t, o, h, l, c}]}.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "agent_data" / "market_data"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

YAHOO = {
    "vix": "%5EVIX",
    "yield_10y": "%5ETNX",
    "yield_13w": "%5EIRX",
    "dollar_index": "DX-Y.NYB",
    "spy": "SPY",
    "qqq": "QQQ",
    "gld": "GLD",
    "slv": "SLV",
    "tlt": "TLT",
    "eem": "EEM",
    "gold_futures": "GC=F",
    "oil_futures": "CL=F",
    "btc_usd": "BTC-USD",
    "eth_usd": "ETH-USD",
}


def yahoo(symbol: str, years: int = 10) -> dict:
    query = urllib.parse.urlencode({"range": f"{years}y", "interval": "1d"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{query}"
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    result = payload["chart"]["result"][0]
    stamps = result.get("timestamp") or []
    quotes = result["indicators"]["quote"][0]
    bars = []
    for index, stamp in enumerate(stamps):
        close = quotes["close"][index]
        if close is None:
            continue
        bars.append({"t": int(stamp) * 1000, "o": quotes["open"][index] or close,
                     "h": quotes["high"][index] or close, "l": quotes["low"][index] or close,
                     "c": float(close)})
    return {"symbol": symbol, "bars": bars}


def deribit_dvol(currency: str = "BTC") -> dict:
    start = int(time.time() * 1000) - 365 * 24 * 3600 * 1000
    end = int(time.time() * 1000)
    url = ("https://www.deribit.com/api/v2/public/get_volatility_index_data"
           f"?currency={currency}&start_timestamp={start}&end_timestamp={end}&resolution=1D")
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    bars = [{"t": int(row[0]), "c": float(row[4])} for row in payload["result"]["data"]]
    return {"symbol": f"{currency}-DVOL", "bars": bars}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--symbols", default=",".join(YAHOO))
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)

    for name in [item.strip() for item in args.symbols.split(",") if item.strip()]:
        if name not in YAHOO:
            print(f"{name}: unknown key")
            continue
        try:
            payload = yahoo(YAHOO[name], years=args.years)
        except Exception as exc:  # noqa: BLE001 - a source can refuse
            print(f"{name}: FAILED {type(exc).__name__}: {exc}")
            continue
        if not payload["bars"]:
            print(f"{name}: empty")
            continue
        (OUT / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
        first = payload["bars"][0]["t"] // 1000
        last = payload["bars"][-1]["t"] // 1000
        print(f"{name:14} {len(payload['bars']):5} bars  "
              f"{time.strftime('%Y-%m-%d', time.gmtime(first))} .. "
              f"{time.strftime('%Y-%m-%d', time.gmtime(last))}")

    for currency in ("BTC", "ETH"):
        try:
            payload = deribit_dvol(currency)
        except Exception as exc:  # noqa: BLE001
            print(f"{currency}-DVOL: FAILED {type(exc).__name__}")
            continue
        (OUT / f"{currency.lower()}_dvol.json").write_text(json.dumps(payload), encoding="utf-8")
        print(f"{currency}-DVOL     {len(payload['bars']):5} bars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
