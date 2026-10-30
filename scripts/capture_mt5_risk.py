#!/usr/bin/env python3
"""Capture a read-only MT5 account-risk snapshot and append it to PostgreSQL."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from critical_consensus.mt5_risk import collect_mt5_risk_snapshot
from critical_consensus.persistence import PostgresRepository


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--day-start-equity", type=float, required=True,
                        help="verified equity at the start of the broker's UTC day")
    parser.add_argument("--equity-high-watermark", type=float, required=True,
                        help="verified intraday/high-water equity baseline")
    parser.add_argument("--risk-fraction", type=float, required=True,
                        help="risk estimate for the proposed request, e.g. 0.002 for 0.2 percent")
    parser.add_argument("--symbol-groups", type=Path, required=True,
                        help="JSON object mapping symbols to correlation/exposure groups")
    parser.add_argument("--terminal-path", help="optional MT5 terminal executable path")
    args = parser.parse_args()
    try:
        groups = json.loads(args.symbol_groups.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(f"cannot read --symbol-groups JSON object: {type(exc).__name__}")
    if not isinstance(groups, dict):
        parser.error("--symbol-groups must contain a JSON object")
    repo = PostgresRepository()
    repo.migrate()
    snapshot = collect_mt5_risk_snapshot(
        symbol=args.symbol, day_start_equity=args.day_start_equity,
        equity_high_watermark=args.equity_high_watermark,
        proposed_risk_fraction=args.risk_fraction, symbol_groups=groups,
        terminal_path=args.terminal_path,
    )
    persisted = repo.record_mt5_risk_snapshot(snapshot)
    print(json.dumps({"snapshot": snapshot, "persistence": persisted}, indent=2, ensure_ascii=False))
    # A DENY result is successfully captured evidence but is deliberately a non-zero CLI status.
    return 0 if snapshot.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
