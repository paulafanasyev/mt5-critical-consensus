#!/usr/bin/env python3
"""Capture a read-only MT5 account-risk snapshot for one exact proposed request."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from critical_consensus.mt5_risk import collect_mt5_risk_snapshot
from critical_consensus.persistence import PostgresRepository


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--direction", required=True, choices=["BUY", "SELL"])
    parser.add_argument("--size", type=float, required=True, help="proposed order volume in MT5 lots")
    parser.add_argument("--expected-price", type=float, required=True, help="exact expected execution price")
    parser.add_argument("--stop-loss", type=float, required=True, help="exact stop-loss price")
    parser.add_argument("--take-profit", type=float, required=True, help="exact take-profit price")
    parser.add_argument("--day-start-equity", type=float, required=True,
                        help="verified, cash-flow-adjusted equity at start of UTC day")
    parser.add_argument("--equity-high-watermark", type=float, required=True,
                        help="verified intraday/high-water equity baseline")
    parser.add_argument("--symbol-groups", type=Path, required=True,
                        help="JSON object mapping every tradable/open-position symbol to an exposure group")
    parser.add_argument("--max-slippage-points", type=int, default=20)
    parser.add_argument("--max-spread-points", type=int, default=20)
    parser.add_argument("--max-quote-age-seconds", type=int, default=15)
    parser.add_argument("--terminal-path", help="optional MT5 terminal executable path")
    args = parser.parse_args()
    try:
        groups = json.loads(args.symbol_groups.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(f"cannot read --symbol-groups JSON object: {type(exc).__name__}")
    if not isinstance(groups, dict):
        parser.error("--symbol-groups must contain a JSON object")

    proposed_request = {
        "instrument": args.symbol.upper(),
        "direction": args.direction,
        "size": args.size,
        "expected_price": args.expected_price,
        "sl_tp": {"sl": args.stop_loss, "tp": args.take_profit},
    }
    repo = PostgresRepository()
    repo.migrate()
    snapshot = collect_mt5_risk_snapshot(
        symbol=args.symbol, day_start_equity=args.day_start_equity,
        equity_high_watermark=args.equity_high_watermark,
        proposed_request=proposed_request, symbol_groups=groups,
        terminal_path=args.terminal_path, max_slippage_points=args.max_slippage_points,
        max_spread_points=args.max_spread_points,
        max_quote_age_seconds=args.max_quote_age_seconds,
    )
    persisted = repo.record_mt5_risk_snapshot(snapshot)
    print(json.dumps({"snapshot": snapshot, "persistence": persisted}, indent=2, ensure_ascii=False))
    # A DENY result is successfully captured evidence but deliberately returns non-zero.
    return 0 if snapshot.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
