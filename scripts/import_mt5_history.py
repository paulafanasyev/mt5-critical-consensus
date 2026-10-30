#!/usr/bin/env python3
"""Import an explicit MT5 CSV export or read-only terminal history into PostgreSQL."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from critical_consensus.market_data import fetch_mt5_bars, read_mt5_csv
from critical_consensus.persistence import PostgresRepository


def parse_aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("time must include a timezone, e.g. 2026-10-01T00:00:00Z")
    return parsed.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", type=Path, help="MT5 OHLC CSV export; the file is not modified")
    source.add_argument("--live-terminal", action="store_true", help="query the installed MT5 terminal read-only")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True, choices=["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"])
    parser.add_argument("--source-timezone", help="required for naive CSV dates, e.g. Europe/Helsinki or UTC")
    parser.add_argument("--from-utc", type=parse_aware, help="required for --live-terminal")
    parser.add_argument("--to-utc", type=parse_aware, help="required for --live-terminal")
    parser.add_argument("--terminal-path", help="optional MetaTrader 5 terminal executable path")
    args = parser.parse_args()

    if args.live_terminal:
        if args.from_utc is None or args.to_utc is None:
            parser.error("--live-terminal requires --from-utc and --to-utc")
        if args.from_utc >= args.to_utc:
            parser.error("--from-utc must be earlier than --to-utc")
        bars = fetch_mt5_bars(args.symbol, args.timeframe, args.from_utc, args.to_utc,
                              terminal_path=args.terminal_path)
        source_name = "MT5 terminal"
    else:
        bars = read_mt5_csv(args.csv, symbol=args.symbol, timeframe=args.timeframe,
                            source_timezone=args.source_timezone)
        source_name = str(args.csv)

    if not bars:
        parser.error("source returned zero market bars; nothing was persisted")
    repo = PostgresRepository()
    repo.migrate()
    result = repo.append_market_bars(bars)
    print(f"source={source_name}; symbol={args.symbol.upper()}; timeframe={args.timeframe}; "
          f"rows={len(bars)}; inserted={result['inserted']}; "
          f"identical_duplicates={result['identical_duplicates']}; conflicting bars are rejected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
