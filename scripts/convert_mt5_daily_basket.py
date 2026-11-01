#!/usr/bin/env python3
"""Convert real MT5 Script-exported D1 bars and verify the frozen broker-data hashes.

The CSV must come from mql5/Scripts/ExportDailyBasket.mq5. No generated or fallback
quotes are accepted. The converter writes agent_data/daily only after every raw JSON
payload matches the original SHA-256, byte count, row count and date range.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "agent_data" / "daily"
FROZEN_MANIFEST = ROOT / "docs" / "evidence" / "daily_cache_manifest.json"
SYMBOLS = ("XAUUSD", "XAGUSD", "EURUSD", "AUDUSD", "NZDUSD")
CSV_FIELDS = ("symbol", "time", "open", "high", "low", "close", "tick_volume", "spread")


class ExportError(RuntimeError):
    """The real broker export is incomplete, mismatched, or unsafe to accept."""


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_status(path: Path) -> None:
    if not path.is_file():
        raise ExportError(f"MT5 status file is missing: {path}")
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if len(lines) < 6 or lines[0] != "PASS" or lines[1] != "DEMO":
        detail = lines[1] if len(lines) > 1 else "no status detail"
        raise ExportError(f"MT5 Script did not confirm a connected DEMO account: {detail}")
    expected_login = os.environ.get("MT5_DEMO_LOGIN", "").strip()
    expected_server = os.environ.get("MT5_DEMO_SERVER", "").strip()
    if not expected_login or lines[2] != expected_login:
        raise ExportError("MT5 Script account login does not match the configured demo login")
    if not expected_server or lines[3] != expected_server:
        raise ExportError("MT5 Script broker server does not match the configured demo server")
    if lines[4] != str(len(SYMBOLS)):
        raise ExportError("MT5 Script did not export the registered five-symbol basket")
    expected_total = sum(int(item["rows"]) for item in json.loads(
        FROZEN_MANIFEST.read_text(encoding="utf-8"))["files"].values())
    if lines[5] != str(expected_total):
        raise ExportError("MT5 Script total bar count does not match the frozen broker manifest")


def load_bars(csv_path: Path) -> dict[str, list[dict]]:
    if not csv_path.is_file():
        raise ExportError(f"MT5 CSV export is missing: {csv_path}")

    basket: dict[str, list[dict]] = {symbol: [] for symbol in SYMBOLS}
    previous_time: dict[str, int] = {symbol: 0 for symbol in SYMBOLS}

    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=";")
            if tuple(reader.fieldnames or ()) != CSV_FIELDS:
                raise ExportError("MT5 CSV header does not match the required export contract")

            for line_number, item in enumerate(reader, start=2):
                symbol = item["symbol"]
                if symbol not in basket:
                    raise ExportError(f"Unexpected symbol in MT5 export at line {line_number}: {symbol}")
                stamp = int(item["time"])
                if stamp <= previous_time[symbol]:
                    raise ExportError(f"{symbol}: duplicate or out-of-order broker bar at line {line_number}")
                previous_time[symbol] = stamp

                open_price = float(item["open"])
                high = float(item["high"])
                low = float(item["low"])
                close = float(item["close"])
                volume = int(item["tick_volume"])
                spread = int(item["spread"])
                prices = (open_price, high, low, close)
                if (not all(math.isfinite(value) and value > 0 for value in prices)
                        or high < low or volume < 0 or spread < 0):
                    raise ExportError(f"{symbol}: invalid OHLCV values at line {line_number}")

                broker_date = datetime.fromtimestamp(stamp, UTC).date().isoformat()
                basket[symbol].append({
                    "t": stamp * 1000,
                    "d": broker_date,
                    "o": open_price,
                    "h": high,
                    "l": low,
                    "c": close,
                    "v": volume,
                    "spread": spread,
                })
    except (OSError, UnicodeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ExportError):
            raise
        raise ExportError(f"Could not parse the MT5 Script export: {exc}") from exc

    return basket


def verify_and_write(basket: dict[str, list[dict]]) -> int:
    if not FROZEN_MANIFEST.is_file():
        raise ExportError(f"Frozen original-data registry is missing: {FROZEN_MANIFEST}")
    frozen = json.loads(FROZEN_MANIFEST.read_text(encoding="utf-8"))
    if tuple(frozen.get("query", {}).get("symbols", ())) != SYMBOLS:
        raise ExportError("Frozen registry symbol order differs from the pre-registered study")
    if frozen.get("query", {}).get("timeframe") != "D1":
        raise ExportError("Frozen registry is not the registered D1 timeframe")

    payloads: dict[str, bytes] = {}
    output_files: dict[str, dict] = {}

    # Verify every symbol completely before creating or changing any research cache file.
    for symbol in SYMBOLS:
        rows = basket[symbol]
        expected = frozen["files"][symbol]
        if len(rows) != int(expected["rows"]):
            raise ExportError(f"{symbol}: row count {len(rows)} != frozen {expected['rows']}")
        first = rows[0]["d"] if rows else ""
        last = rows[-1]["d"] if rows else ""
        if first != expected["first"] or last != expected["last"]:
            raise ExportError(f"{symbol}: date range {first}..{last} != frozen "
                              f"{expected['first']}..{expected['last']}")

        payload = json.dumps(rows, separators=(",", ":")).encode("utf-8")
        actual_hash = sha256(payload)
        if actual_hash != expected["sha256"]:
            raise ExportError(f"{symbol}: real MT5 bars do not match the frozen SHA-256 "
                              f"(actual {actual_hash}, expected {expected['sha256']})")
        if len(payload) != int(expected["bytes"]):
            raise ExportError(f"{symbol}: serialized byte count {len(payload)} "
                              f"!= frozen {expected['bytes']}")

        payloads[symbol] = payload
        output_files[symbol] = {
            "rows": len(rows),
            "first": first,
            "last": last,
            "sha256": actual_hash,
            "bytes": len(payload),
        }
        print(f"VERIFIED {symbol}: {len(rows)} real broker bars; SHA-256 exact")

    parent = CACHE.parent
    parent.mkdir(parents=True, exist_ok=True)
    if CACHE.exists():
        raise ExportError(f"Refusing to overwrite existing research evidence: {CACHE}")

    manifest = {
        "query": frozen["query"],
        "files": output_files,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }

    with tempfile.TemporaryDirectory(prefix="mt5-d1-", dir=parent) as temp_name:
        staged = Path(temp_name)
        for symbol, payload in payloads.items():
            (staged / f"{symbol}.json").write_bytes(payload)
        (staged / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        CACHE.mkdir(parents=True, exist_ok=False)
        for symbol in SYMBOLS:
            os.replace(staged / f"{symbol}.json", CACHE / f"{symbol}.json")
        os.replace(staged / "manifest.json", CACHE / "manifest.json")

    total_rows = sum(len(basket[symbol]) for symbol in SYMBOLS)
    print(f"FROZEN MT5 CACHE VERIFIED: {total_rows} original broker bars; no substitutions")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path, help="CSV written by ExportDailyBasket.mq5")
    parser.add_argument("--status", required=True, type=Path, help="Status file written by ExportDailyBasket.mq5")
    args = parser.parse_args()

    try:
        read_status(args.status)
        basket = load_bars(args.csv)
        return verify_and_write(basket)
    except (ExportError, OSError, json.JSONDecodeError) as exc:
        print(f"NOT_PROVEN: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
