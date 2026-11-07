#!/usr/bin/env python3
"""The Monday long: buy five FX pairs at the weekly open, close at the weekly close.

The scan over every asset found that the first day of the week is systematically positive
on several instruments, and on the FX pairs it held in both halves of the sample:

  GBPUSD +0.148%/day (t=4.89, halves +2.86 / +4.05)
  AUDUSD +0.174%/day (t=4.78, halves +2.56 / +4.22)
  USDCHF +0.100%/day (t=2.96)
  EURUSD +0.077%/day (t=2.54)
  USDJPY +0.081%/day (t=2.18)

This is a separate trade from the gap fill: it is unconditional, entering at the Monday
open whatever the weekend gap was, and it is sized to be visible on the demo account
(0.05 lots per pair, roughly 30-60 USD of risk per pair).

Demo only; the account mode is checked before every order.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "agent_data" / "monday_long.jsonl"
SYMBOLS = ("GBPUSD", "AUDUSD", "USDCHF", "EURUSD", "USDJPY")
VOLUME = 0.05
MAGIC = 8172031
EXPECTED_LOGIN = 114929502
SERVER_OFFSET = 2
ENTRY_SERVER_HOUR = 0     # Monday 00:00 server = Sunday 22:00 UTC
EXIT_SERVER_HOUR = 21     # Monday 21:00 server = 19:00 UTC


def log(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")
    print(f"[{event['ts'][11:19]}] {event['event']}", flush=True)


def main(argv: list[str] | None = None) -> int:
    import MetaTrader5 as mt5

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-sec", type=int, default=60)
    args = parser.parse_args(argv)
    if not mt5.initialize():
        raise SystemExit("MetaTrader5.initialize failed")
    account = mt5.account_info()
    if account is None or account.login != EXPECTED_LOGIN:
        raise SystemExit(f"unexpected account {account.login if account else None}")
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        raise SystemExit("refusing a non-demo account")
    for symbol in SYMBOLS:
        mt5.symbol_select(symbol, True)
    log({"event": f"START Monday long on {', '.join(SYMBOLS)} at {VOLUME} lots"})
    opened_week = None

    while True:
        try:
            now = datetime.now(timezone.utc)
            server = now + timedelta(hours=SERVER_OFFSET)
            positions = [p for p in (mt5.positions_get() or []) if p.magic == MAGIC]

            if (server.weekday() == 0 and server.hour == ENTRY_SERVER_HOUR
                    and opened_week != now.date() and not positions):
                opened_week = now.date()
                for symbol in SYMBOLS:
                    tick = mt5.symbol_info_tick(symbol)
                    if not tick or not tick.ask:
                        continue
                    result = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL,
                                             "symbol": symbol, "volume": VOLUME,
                                             "type": mt5.ORDER_TYPE_BUY, "price": tick.ask,
                                             "deviation": 20, "magic": MAGIC,
                                             "type_filling": mt5.ORDER_FILLING_FOK,
                                             "comment": "monday long"})
                    log({"event": f"{symbol}: BUY {VOLUME} @ {tick.ask:.5f} retcode "
                                  f"{None if result is None else result.retcode}"})

            if positions and server.hour >= EXIT_SERVER_HOUR:
                for position in positions:
                    tick = mt5.symbol_info_tick(position.symbol)
                    result = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL,
                                             "symbol": position.symbol,
                                             "volume": position.volume,
                                             "type": mt5.ORDER_TYPE_SELL,
                                             "position": position.ticket, "price": tick.bid,
                                             "deviation": 20, "magic": MAGIC,
                                             "type_filling": mt5.ORDER_FILLING_FOK,
                                             "comment": "monday long exit"})
                    log({"event": f"{position.symbol}: CLOSE P/L {position.profit:+.2f} "
                                  f"retcode {None if result is None else result.retcode}"})
        except Exception as exc:  # noqa: BLE001
            log({"event": f"cycle error {type(exc).__name__}: {exc}"})
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
