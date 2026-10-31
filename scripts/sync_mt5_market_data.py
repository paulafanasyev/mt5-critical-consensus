#!/usr/bin/env python3
"""Incrementally import only closed MT5 bars and resolve due objective forecasts."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from critical_consensus.market_data import fetch_mt5_bars, retain_completed_bars
from critical_consensus.persistence import PostgresRepository


def parse_aware(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone, e.g. 2026-10-01T00:00:00Z")
    return parsed.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True,
                        choices=["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"])
    parser.add_argument("--bootstrap-from-utc", type=parse_aware,
                        help="required only when this symbol/timeframe has no persisted history")
    parser.add_argument("--terminal-path", help="optional MetaTrader 5 terminal executable path")
    parser.add_argument("--resolve-limit", type=int, default=500)
    parser.add_argument("--finalization-grace-seconds", type=float, default=2.0)
    args = parser.parse_args()
    if (not isinstance(args.finalization_grace_seconds, (int, float))
            or not 0 <= args.finalization_grace_seconds <= 60):
        parser.error("--finalization-grace-seconds must be finite and in [0,60]")

    repo = PostgresRepository()
    repo.migrate()
    symbol = args.symbol.strip().upper()
    existing = repo.get_recent_market_bars(symbol, args.timeframe, limit=21)
    if existing:
        from_utc = existing[-1].time
    elif args.bootstrap_from_utc is not None:
        from_utc = args.bootstrap_from_utc
    else:
        parser.error("no persisted history; provide --bootstrap-from-utc for the initial chronological import")

    to_utc = datetime.now(timezone.utc)
    if from_utc >= to_utc:
        parser.error("last persisted bar timestamp is not earlier than current UTC time")

    fetched = fetch_mt5_bars(
        symbol, args.timeframe, from_utc, to_utc, terminal_path=args.terminal_path,
    )
    received = len(fetched)
    finalized = []
    if fetched:
        try:
            finalized = retain_completed_bars(
                fetched, min_bars=1,
                finalization_grace_seconds=args.finalization_grace_seconds,
                now=to_utc,
            )
        except ValueError as exc:
            if "insufficient completed bars" not in str(exc):
                raise
    dropped_forming = received - len(finalized)
    ingest = repo.append_market_bars(finalized) if finalized else {
        "inserted": 0, "identical_duplicates": 0, "total": 0,
    }
    resolution = repo.resolve_due_market_forecasts(
        symbol=symbol, timeframe=args.timeframe,
        limit=args.resolve_limit,
        finalization_grace_seconds=args.finalization_grace_seconds,
        now=to_utc,
    )
    print(json.dumps({
        "symbol": symbol,
        "timeframe": args.timeframe,
        "from_utc": from_utc.isoformat(),
        "to_utc": to_utc.isoformat(),
        "received_bars": received,
        "finalized_bars": len(finalized),
        "dropped_forming_bars": dropped_forming,
        "ingestion": ingest,
        "forecast_resolution": resolution,
        "trade_allowed": False,
        "execution": "NOT_REQUESTED",
    }, indent=2, sort_keys=True, default=str, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
