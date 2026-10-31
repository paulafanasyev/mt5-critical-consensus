#!/usr/bin/env python3
"""Read-only smoke test for a logged-in MetaTrader 5 demo terminal.

This module initializes MT5, verifies the exact requested demo account, reads terminal
connectivity and selected-symbol quotes, and shuts the terminal connection down. It has
no order submission, position modification, or trade-management calls.
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

UTC = timezone.utc
SYMBOL_PATTERN = re.compile(r"[A-Z0-9._-]{1,32}\\Z")


class ProbeError(RuntimeError):
    """A sanitized, non-secret terminal probe failure."""


def _finite_positive(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProbeError(f"{name} is missing or not numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ProbeError(f"{name} is not a finite positive number")
    return number


def _timestamp_utc(tick: Any) -> datetime:
    stamp = getattr(tick, "time_msc", None)
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp <= 0:
        stamp = getattr(tick, "time", None)
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp <= 0:
        raise ProbeError("MT5 quote has no usable timestamp")
    seconds = float(stamp) / 1000.0 if float(stamp) > 1e12 else float(stamp)
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ProbeError("MT5 quote timestamp is outside the supported range") from exc


def run_probe(
    mt5: Any,
    *,
    login: int,
    password: str,
    server: str,
    symbols: Iterable[str],
    terminal_path: str | None = None,
) -> dict[str, Any]:
    """Connect to one specified demo account and collect read-only terminal evidence."""
    if isinstance(login, bool) or not isinstance(login, int) or login <= 0:
        raise ProbeError("MT5 demo login must be a positive integer")
    if not isinstance(password, str) or not password:
        raise ProbeError("MT5 demo password is missing")
    if not isinstance(server, str) or not server.strip():
        raise ProbeError("MT5 demo server is missing")
    normalized = [str(symbol).strip().upper() for symbol in symbols]
    if not normalized or len(normalized) > 20:
        raise ProbeError("Supply between 1 and 20 symbols")
    if len(set(normalized)) != len(normalized) or any(
        not SYMBOL_PATTERN.fullmatch(symbol) for symbol in normalized
    ):
        raise ProbeError("Symbols must be unique MT5 symbol names using A-Z, 0-9, dot, underscore or dash")
    if terminal_path and not Path(terminal_path).is_file():
        raise ProbeError("The requested MT5 terminal executable was not found")

    kwargs: dict[str, Any] = {
        "login": login,
        "password": password,
        "server": server.strip(),
        "timeout": 60_000,
    }
    if terminal_path:
        kwargs["path"] = terminal_path

    try:
        initialized = bool(mt5.initialize(**kwargs))
    except Exception as exc:
        raise ProbeError("MetaTrader5.initialize raised an exception; credentials were not logged") from None
    if not initialized:
        code = -1
        try:
            error = mt5.last_error()
            if isinstance(error, (tuple, list)) and error and isinstance(error[0], int):
                code = error[0]
        except Exception:
            pass
        raise ProbeError(f"MetaTrader 5 initialization failed (error code {code})")

    try:
        terminal = mt5.terminal_info()
        if terminal is None or getattr(terminal, "connected", False) is not True:
            raise ProbeError("MT5 terminal is not connected to the broker")

        account = mt5.account_info()
        if account is None:
            raise ProbeError("MT5 returned no account information")
        actual_login = getattr(account, "login", None)
        if isinstance(actual_login, bool) or not isinstance(actual_login, int) or actual_login != login:
            raise ProbeError("Connected MT5 account does not match the configured demo login")
        actual_server = getattr(account, "server", None)
        if not isinstance(actual_server, str) or actual_server != server.strip():
            raise ProbeError("Connected MT5 account does not match the configured broker server")
        demo_mode = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", None)
        actual_mode = getattr(account, "trade_mode", None)
        if demo_mode is None or actual_mode != demo_mode:
            raise ProbeError("Connected account is not a MetaTrader DEMO account")

        quote_rows: list[dict[str, Any]] = []
        for symbol in normalized:
            info = mt5.symbol_info(symbol)
            if info is None:
                if not mt5.symbol_select(symbol, True):
                    raise ProbeError(f"MT5 could not select symbol {symbol}")
                info = mt5.symbol_info(symbol)
            if info is None:
                raise ProbeError(f"MT5 returned no symbol metadata for {symbol}")
            if getattr(info, "visible", True) is not True:
                if not mt5.symbol_select(symbol, True):
                    raise ProbeError(f"MT5 could not make symbol {symbol} visible")

            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                raise ProbeError(f"MT5 returned no quote for {symbol}")
            bid = _finite_positive(getattr(tick, "bid", None), f"{symbol} bid")
            ask = _finite_positive(getattr(tick, "ask", None), f"{symbol} ask")
            if ask < bid:
                raise ProbeError(f"{symbol} ask is below bid")
            observed_at = _timestamp_utc(tick)
            quote_rows.append({
                "symbol": symbol,
                "bid": bid,
                "ask": ask,
                "spread": ask - bid,
                "quote_time_utc": observed_at.isoformat(),
                "quote_age_seconds": round((datetime.now(UTC) - observed_at).total_seconds(), 3),
            })

        return {
            "status": "PASS",
            "mode": "DEMO",
            "broker_server": server.strip(),
            "terminal_connected": True,
            "terminal_trade_allowed": bool(getattr(terminal, "trade_allowed", False)),
            "account_trade_allowed": bool(getattr(account, "trade_allowed", False)),
            "quotes": quote_rows,
            "orders_submitted": 0,
            "positions_modified": 0,
            "note": "Read-only connectivity probe; quote age is reported, not treated as a freshness approval.",
        }
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terminal-path", required=True, help="path to terminal64.exe")
    parser.add_argument("--symbols", default="EURUSD", help="comma-separated symbols, default EURUSD")
    args = parser.parse_args(argv)

    try:
        login_text = os.environ.get("MT5_DEMO_LOGIN", "").strip()
        if not login_text.isdigit() or int(login_text) <= 0:
            raise ProbeError("MT5_DEMO_LOGIN must be set to a positive integer")
        password = os.environ.get("MT5_DEMO_PASSWORD", "")
        server = os.environ.get("MT5_DEMO_SERVER", "").strip()
        if not password or not server:
            raise ProbeError("MT5_DEMO_PASSWORD and MT5_DEMO_SERVER must be configured as Actions secrets")
        mt5 = importlib.import_module("MetaTrader5")
        report = run_probe(
            mt5,
            login=int(login_text),
            password=password,
            server=server,
            symbols=args.symbols.split(","),
            terminal_path=args.terminal_path,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except ProbeError as exc:
        print(json.dumps({"status": "NOT_PROVEN", "reason": str(exc), "orders_submitted": 0},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except ImportError:
        print(json.dumps({"status": "NOT_PROVEN", "reason": "Python MetaTrader5 package is not installed",
                          "orders_submitted": 0}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
