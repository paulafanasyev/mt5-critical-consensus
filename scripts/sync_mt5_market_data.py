#!/usr/bin/env python3
"""Incrementally import only closed MT5 bars and resolve due objective forecasts."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from critical_consensus.market_data import (
    detect_potential_bar_gaps,
    fetch_mt5_bars,
    retain_completed_bars,
)
from critical_consensus.persistence import PostgresRepository


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


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
    parser.add_argument("--resolve-limit", type=positive_int, default=500)
    parser.add_argument(
        "--ignore-weekend-closures", action="store_true",
        help="ignore weekend-spanning gaps only when this instrument's broker session is verified to close on weekends",
    )
    parser.add_argument("--finalization-grace-seconds", type=float, default=2.0)
    args = parser.parse_args()
    if (not isinstance(args.finalization_grace_seconds, (int, float))
            or not 0 <= args.finalization_grace_seconds <= 60):
        parser.error("--finalization-grace-seconds must be finite and in [0,60]")

    symbol = args.symbol.strip().upper()
    if not symbol:
        parser.error("--symbol must not be empty")

    repo = PostgresRepository()
    repo.migrate()
    # Scan the recent immutable history window for suspicious gaps while resuming
    # from its newest bar. Gaps are advisory because broker sessions/holidays vary.
    existing = repo.get_recent_market_bars(symbol, args.timeframe, limit=5000)
    if existing:
        from_utc = existing[-1].time
    elif args.bootstrap_from_utc is not None:
        from_utc = args.bootstrap_from_utc
    else:
        parser.error("no persisted history; provide --bootstrap-from-utc for the initial chronological import")

    application_clock_utc = datetime.now(timezone.utc)
    database_clock = repo.get_database_clock()
    to_utc = database_clock["now_utc"]
    clock_skew_seconds = abs((application_clock_utc - to_utc).total_seconds())
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
    latest_persisted = existing[-1].time if existing else None
    new_finalized = [
        bar for bar in finalized
        if latest_persisted is None or bar.time > latest_persisted
    ]
    gap_scan_bars = existing + new_finalized
    potential_gaps = (
        detect_potential_bar_gaps(
            gap_scan_bars, ignore_weekend_closures=args.ignore_weekend_closures,
        )
        if len(gap_scan_bars) >= 2 else []
    )
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
        "clock": {
            "database_time_utc": to_utc.isoformat(),
            "database_session_timezone": database_clock["session_timezone"],
            "application_time_utc": application_clock_utc.isoformat(),
            "application_database_skew_seconds": round(clock_skew_seconds, 3),
            "finalization_clock_source": "postgresql_server_clock",
        },
        "received_bars": received,
        "finalized_bars": len(finalized),
        "dropped_forming_bars": dropped_forming,
        "ingestion": ingest,
        "forecast_resolution": resolution,
        "market_history_integrity": {
            "status": (
                "POTENTIAL_GAPS_REQUIRING_REVIEW"
                if potential_gaps else "NO_SUSPICIOUS_GAPS_IN_SCANNED_WINDOW"
            ),
            "bars_scanned": len(gap_scan_bars),
            "scan_from_utc": gap_scan_bars[0].time.isoformat() if gap_scan_bars else None,
            "scan_to_utc": gap_scan_bars[-1].time.isoformat() if gap_scan_bars else None,
            "scan_scope": (
                "last_5000_persisted_bars_plus_new_closed_bars"
                if existing else "initial_bootstrap_batch_only"
            ),
            "potential_gaps": potential_gaps,
            "interpretation": (
                "Advisory only. Weekend-spanning gaps are included unless "
                "--ignore-weekend-closures is explicitly set after verifying the broker session; "
                "that option suppresses only normal weekend-sized intervals (timeframe plus "
                "three days), while extended gaps remain visible."
            ),
            "weekend_gaps_ignored": args.ignore_weekend_closures,
            "automatic_backfill": False,
        },
        "trade_allowed": False,
        "execution": "NOT_REQUESTED",
    }, indent=2, sort_keys=True, default=str, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
