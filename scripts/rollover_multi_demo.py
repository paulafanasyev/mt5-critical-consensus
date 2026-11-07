#!/usr/bin/env python3
"""Agent: the daily rollover recovery on three pairs.

The intraday scan showed the first hour of the server day is positive on several USD pairs
with t between 7 and 11. That may be a real rollover effect or a quoting artefact of this
server, which is why this agent exists: it buys at the first hour and sells at the end of
it, with real fills, on three pairs at once.

Entry 22:00 UTC (00:00 server), exit 22:55 UTC. 0.02 lots per pair. Demo only.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "agent_data" / "rollover_multi.jsonl"
SYMBOLS = ("AUDUSD", "GBPUSD")  # USDCHF is covered by rollover_demo.py
VOLUME = 0.02
MAGIC = 8172033
EXPECTED_LOGIN = 114929502
ENTRY_HOUR = 22
EXIT_HOUR = 23


def log(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")
    print(f"[{event['ts'][11:19]}] {event['event']}", flush=True)


def main(argv: list[str] | None = None) -> int:
    import MetaTrader5 as mt5

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-sec", type=int, default=30)
    args = parser.parse_args(argv)
    if not mt5.initialize():
        raise SystemExit("MetaTrader5.initialize failed")
    account = mt5.account_info()
    if account is None or account.login != EXPECTED_LOGIN:
        raise SystemExit("unexpected account")
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        raise SystemExit("refusing a non-demo account")
    for symbol in SYMBOLS:
        mt5.symbol_select(symbol, True)
    log({"event": f"START rollover multi agent on {', '.join(SYMBOLS)}"})
    opened = None

    while True:
        try:
            now = datetime.now(timezone.utc)
            positions = [p for p in (mt5.positions_get() or []) if p.magic == MAGIC]
            if not positions and now.hour == ENTRY_HOUR and opened != now.date():
                opened = now.date()
                for symbol in SYMBOLS:
                    tick = mt5.symbol_info_tick(symbol)
                    if not tick or not tick.ask:
                        continue
                    result = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL,
                                             "symbol": symbol, "volume": VOLUME,
                                             "type": mt5.ORDER_TYPE_BUY, "price": tick.ask,
                                             "deviation": 20, "magic": MAGIC,
                                             "type_filling": mt5.ORDER_FILLING_FOK,
                                             "comment": "rollover multi"})
                    log({"event": f"{symbol}: BUY {VOLUME} @ {tick.ask:.5f} retcode "
                                  f"{None if result is None else result.retcode}"})
            if positions and now.hour == EXIT_HOUR:
                for position in positions:
                    tick = mt5.symbol_info_tick(position.symbol)
                    result = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL,
                                             "symbol": position.symbol,
                                             "volume": position.volume,
                                             "type": mt5.ORDER_TYPE_SELL,
                                             "position": position.ticket, "price": tick.bid,
                                             "deviation": 20, "magic": MAGIC,
                                             "type_filling": mt5.ORDER_FILLING_FOK,
                                             "comment": "rollover multi exit"})
                    log({"event": f"{position.symbol}: CLOSE P/L {position.profit:+.2f} "
                                  f"retcode {None if result is None else result.retcode}"})
        except Exception as exc:  # noqa: BLE001
            log({"event": f"cycle error {type(exc).__name__}: {exc}"})
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
