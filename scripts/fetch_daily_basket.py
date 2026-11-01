#!/usr/bin/env python3
"""Cache the daily bars the 2026-11 candidate study is pre-registered on.

The study must be reproducible after the terminal is gone, so this script writes one JSON
file per symbol plus a manifest carrying a sha256 of every file, the fetch time and the
exact query. Evaluation (`scripts/candidate_study.py`) reads only the cache; it never
talks to the terminal.

Bar timestamps from MT5 are broker wall-clock seconds since the epoch. For daily bars the
only thing the study uses is the broker calendar date, so no server offset is applied and
no decision can depend on it.

Usage:
    python scripts/fetch_daily_basket.py            # create the cache, refuse to overwrite
    python scripts/fetch_daily_basket.py --check    # verify the cache against the manifest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "agent_data" / "daily"
START = datetime(2004, 1, 1, tzinfo=UTC)
END = datetime(2026, 11, 2, tzinfo=UTC)
SYMBOLS = ("XAUUSD", "XAGUSD", "EURUSD", "AUDUSD", "NZDUSD")
TIMEFRAME = "D1"


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def fetch(symbol: str) -> list[dict]:
    import MetaTrader5 as mt5

    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"{symbol}: the broker terminal refused to select the symbol")
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_D1, START, END)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"{symbol}: copy_rates_range returned nothing ({mt5.last_error()})")
    rows = []
    for rate in rates:
        stamp = datetime.fromtimestamp(int(rate["time"]), UTC)
        rows.append({
            "t": int(rate["time"]) * 1000,
            "d": stamp.date().isoformat(),
            "o": float(rate["open"]),
            "h": float(rate["high"]),
            "l": float(rate["low"]),
            "c": float(rate["close"]),
            "v": int(rate["tick_volume"]),
            "spread": int(rate["spread"]),
        })
    return rows


def write_cache() -> int:
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise SystemExit(f"MetaTrader5.initialize failed: {mt5.last_error()}")
    CACHE.mkdir(parents=True, exist_ok=True)
    manifest_path = CACHE / "manifest.json"
    if manifest_path.exists():
        raise SystemExit(f"{manifest_path} already exists; the cache is evidence and is never overwritten")
    manifest: dict[str, dict] = {"query": {"start": START.isoformat(), "end": END.isoformat(),
                                           "timeframe": TIMEFRAME, "symbols": list(SYMBOLS)},
                                 "files": {}}
    try:
        for symbol in SYMBOLS:
            rows = fetch(symbol)
            payload = json.dumps(rows, separators=(",", ":")).encode()
            (CACHE / f"{symbol}.json").write_bytes(payload)
            manifest["files"][symbol] = {
                "rows": len(rows), "first": rows[0]["d"], "last": rows[-1]["d"],
                "sha256": digest(payload), "bytes": len(payload),
            }
            print(f"{symbol:8} {len(rows):6} bars  {rows[0]['d']} .. {rows[-1]['d']}")
    finally:
        mt5.shutdown()
    manifest["fetched_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"manifest: {manifest_path}")
    return 0


def check_cache() -> int:
    manifest_path = CACHE / "manifest.json"
    if not manifest_path.exists():
        print("no manifest; the cache has not been fetched", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failures = 0
    for symbol, entry in manifest["files"].items():
        payload = (CACHE / f"{symbol}.json").read_bytes()
        actual = digest(payload)
        status = "ok" if actual == entry["sha256"] else "MISMATCH"
        failures += actual != entry["sha256"]
        print(f"{symbol:8} {status} {entry['rows']:6} bars  {entry['first']} .. {entry['last']}")
    print("cache verified" if not failures else f"{failures} file(s) changed")
    return 0 if not failures else 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the cache instead of fetching it")
    args = parser.parse_args(argv)
    return check_cache() if args.check else write_cache()


if __name__ == "__main__":
    raise SystemExit(main())
