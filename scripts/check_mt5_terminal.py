#!/usr/bin/env python3
"""Read-only preflight for a configured Windows MetaTrader 5 terminal."""
from __future__ import annotations

import argparse
import importlib
import json
from datetime import datetime, timezone


TIMEFRAMES = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
    "W1": "TIMEFRAME_W1",
}


def probe_terminal(symbol: str, timeframe: str, terminal_path: str | None = None) -> dict:
    symbol = symbol.strip().upper()
    timeframe = timeframe.strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"unsupported timeframe: {timeframe}")

    try:
        mt5 = importlib.import_module("MetaTrader5")
    except ImportError as exc:
        raise RuntimeError("MetaTrader5 Python package is not installed in this interpreter") from exc

    initialized = mt5.initialize(path=terminal_path) if terminal_path else mt5.initialize()
    if not initialized:
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

    try:
        terminal = mt5.terminal_info()
        if terminal is None:
            raise RuntimeError(f"MT5 terminal_info failed: {mt5.last_error()}")
        if not bool(getattr(terminal, "connected", False)):
            raise RuntimeError("MT5 terminal is open but not connected to a trade server")

        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"MT5 could not select symbol {symbol}: {mt5.last_error()}")
        timeframe_constant = getattr(mt5, TIMEFRAMES[timeframe], None)
        if timeframe_constant is None:
            raise RuntimeError(f"installed MT5 package does not expose timeframe {timeframe}")

        # start_pos=1 deliberately skips bar 0, which is the still-forming candle.
        rates = mt5.copy_rates_from_pos(symbol, timeframe_constant, 1, 2)
        if rates is None:
            raise RuntimeError(f"MT5 closed-bar history query failed: {mt5.last_error()}")
        if len(rates) < 2:
            raise RuntimeError(
                f"MT5 returned only {len(rates)} closed bars; load more chart history and retry"
            )

        last_closed = rates[-1]
        timestamp = int(last_closed["time"])
        return {
            "status": "PASS",
            "terminal_connected": True,
            "terminal_name": str(getattr(terminal, "name", "unknown")),
            "terminal_build": int(getattr(terminal, "build", 0) or 0),
            "symbol": symbol,
            "timeframe": timeframe,
            "closed_bars_checked": len(rates),
            "latest_closed_bar_utc": datetime.fromtimestamp(
                timestamp, tz=timezone.utc
            ).isoformat(),
            "trading_api_called": False,
            "order_submission": "NOT_REQUESTED",
        }
    finally:
        mt5.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True, choices=sorted(TIMEFRAMES))
    parser.add_argument("--terminal-path")
    args = parser.parse_args()
    try:
        result = probe_terminal(args.symbol, args.timeframe, args.terminal_path)
    except (RuntimeError, ValueError) as exc:
        parser.exit(2, f"MT5 PREFLIGHT FAILED: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
