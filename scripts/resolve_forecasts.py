#!/usr/bin/env python3
"""Resolve due market-event forecasts using only immutable persisted MT5 bars."""
from __future__ import annotations

import argparse
import json

from critical_consensus.persistence import PostgresRepository


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default=None, help="Optional symbol filter, e.g. EURUSD")
    parser.add_argument("--timeframe", default=None,
                        choices=["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"])
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--finalization-grace-seconds", type=float, default=2.0)
    args = parser.parse_args()
    repo = PostgresRepository()
    repo.migrate()
    result = repo.resolve_due_market_forecasts(
        symbol=args.symbol,
        timeframe=args.timeframe,
        limit=args.limit,
        finalization_grace_seconds=args.finalization_grace_seconds,
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    # Pending outcomes are expected while the target bar has not yet closed.
    # This command never creates labels when no eligible future bar is available.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
