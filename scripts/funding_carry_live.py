#!/usr/bin/env python3
"""Live paper tracker for the delta-neutral funding carry.

Every cycle it reads the funding history of BTCUSDT and ETHUSDT from Bybit, accrues every
funding period that closed after the paper position was opened, and logs the resulting P/L,
the annualised rate and the spot-perp basis. No account, no orders: the position is paper.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "agent_data" / "funding_carry_live.jsonl"
STATE = ROOT / "agent_data" / "funding_carry_state.json"
NOTIONAL = 1_000.0
FEE_PER_ASSET = 3.10
SYMBOLS = ("BTCUSDT", "ETHUSDT")


def get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "dsh"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def funding(symbol: str) -> list[tuple[int, float]]:
    payload = get(f"https://api.bybit.com/v5/market/funding/history?category=linear"
                  f"&symbol={symbol}&limit=200")
    rows = [(int(row["fundingRateTimestamp"]), float(row["fundingRate"]))
            for row in payload["result"]["list"]]
    return sorted(rows)


def prices(symbol: str) -> tuple[float, float]:
    spot = get(f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={symbol}")
    perp = get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}")
    return (float(spot["result"]["list"][0]["lastPrice"]),
            float(perp["result"]["list"][0]["lastPrice"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-sec", type=int, default=300)
    args = parser.parse_args(argv)

    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else None
    if state is None:
        now = int(time.time() * 1000)
        state = {"opened_at": now, "assets": {}, "capital": NOTIONAL * len(SYMBOLS)}
        for symbol in SYMBOLS:
            state["assets"][symbol] = {"last_ts": now, "accrued": 0.0, "periods": 0}
        STATE.write_text(json.dumps(state), encoding="utf-8")
        print(f"paper position opened at {datetime.fromtimestamp(now / 1000, tz=timezone.utc)}",
              flush=True)

    while True:
        total = 0.0
        report = {"ts": datetime.now(timezone.utc).isoformat(), "assets": {}}
        for symbol in SYMBOLS:
            asset = state["assets"][symbol]
            history = funding(symbol)
            fresh = [rate for ts, rate in history if ts > asset["last_ts"]]
            if fresh:
                asset["accrued"] += sum(NOTIONAL * rate for rate in fresh)
                asset["last_ts"] = max(ts for ts, _ in history)
                asset["periods"] += len(fresh)
            spot, perp = prices(symbol)
            basis_bps = (perp / spot - 1) * 10_000
            net = asset["accrued"] - FEE_PER_ASSET
            total += net
            report["assets"][symbol] = {"accrued": round(asset["accrued"], 4),
                                        "periods": asset["periods"],
                                        "net": round(net, 4),
                                        "basis_bps": round(basis_bps, 2)}
        report["total_net"] = round(total, 4)
        report["capital"] = state["capital"]
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report) + "\n")
        print(f"[{report['ts'][11:19]}] net ${total:+.2f} "
              + " ".join(f"{symbol}: ${data['net']:+.2f} ({data['periods']}p, "
                         f"basis {data['basis_bps']:+.1f}bps)"
                         for symbol, data in report["assets"].items()), flush=True)
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
