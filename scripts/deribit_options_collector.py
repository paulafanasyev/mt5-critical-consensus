#!/usr/bin/env python3
"""Collect live crypto option quotes so the volatility premium can be tested on real prices.

Deribit publishes its whole option chain through the public API: one call returns bid, ask,
mark price and implied volatility for every strike and expiry. Snapshotting that chain every
hour builds the dataset needed to sell volatility in a paper account and see whether the
premium measured from the index (DVOL) is really capturable at quoted prices.

No account, no key, no orders. Output: agent_data/options/<currency>_<timestamp>.jsonl
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "agent_data" / "options"


def get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "dsh"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def snapshot(currency: str) -> list[dict]:
    payload = get("https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
                  f"?currency={currency}&kind=option")
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for item in payload["result"]:
        parts = item["instrument_name"].split("-")
        if len(parts) != 4:
            continue
        _, expiry, strike, kind = parts
        rows.append({"ts": now, "instrument": item["instrument_name"],
                     "expiry": expiry, "strike": float(strike), "kind": kind,
                     "bid": item.get("bid_price"), "ask": item.get("ask_price"),
                     "mark": item.get("mark_price"), "mid_iv": item.get("mark_iv"),
                     "underlying": item.get("underlying_price"),
                     "volume": item.get("volume"), "open_interest": item.get("open_interest")})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-sec", type=int, default=3600)
    parser.add_argument("--currencies", default="BTC,ETH")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)

    while True:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H")
        total = 0
        for currency in [item.strip() for item in args.currencies.split(",") if item.strip()]:
            try:
                rows = snapshot(currency)
            except Exception as exc:  # noqa: BLE001 - the venue can refuse a call
                print(f"{currency}: {type(exc).__name__}: {exc}", flush=True)
                continue
            path = OUT / f"{currency.lower()}_{stamp}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
            total += len(rows)
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] snapshotted {total} option quotes",
              flush=True)
        if args.once:
            break
        time.sleep(args.interval_sec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
