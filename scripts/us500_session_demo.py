#!/usr/bin/env python3
"""Trade the US500 session drift on the MT5 demo account.

The only statistically supported rule this venue can express: on the broker's own data the
US500 rises during the 14:00-20:00 UTC session by +0.053% per day (t = +2.29, 850 days),
which survives the 0.66 bps spread. This script buys at the session open and closes at the
session close, one position at a time, with a hard stop, on the demo account only.

It talks to the terminal directly through the MetaTrader5 package rather than through the
EA bridge: the rule needs timed entries and exits, which the bridge's gate interface does
not express. Demo only; the account check refuses anything that is not a demo login.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "agent_data" / "us500_session.jsonl"
SYMBOL = "US500"
VOLUME = 1.0
STOP_FRACTION = 0.015
ENTRY_HOUR = 14
EXIT_HOUR = 20
MAGIC = 8172027
EXPECTED_LOGIN = 114929502


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
        raise SystemExit(f"unexpected account: {account.login if account else None}")
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        raise SystemExit("refusing to trade a non-demo account")
    mt5.symbol_select(SYMBOL, True)
    log({"event": f"START session rule on {SYMBOL}, balance {account.balance:.2f}, "
                  f"entry {ENTRY_HOUR}:00-{EXIT_HOUR}:00 UTC, volume {VOLUME}"})

    while True:
        try:
            now = datetime.now(timezone.utc)
            positions = [p for p in (mt5.positions_get(symbol=SYMBOL) or []) if p.magic == MAGIC]
            in_session = ENTRY_HOUR <= now.hour < EXIT_HOUR

            if positions and now.hour >= EXIT_HOUR:
                for position in positions:
                    tick = mt5.symbol_info_tick(SYMBOL)
                    result = mt5.order_send({
                        "action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL,
                        "volume": position.volume,
                        "type": mt5.ORDER_TYPE_SELL if position.type == 0 else mt5.ORDER_TYPE_BUY,
                        "position": position.ticket, "price": tick.bid if position.type == 0 else tick.ask,
                        "deviation": 30, "magic": MAGIC, "type_filling": mt5.ORDER_FILLING_FOK})
                    log({"event": f"CLOSE ticket {position.ticket} retcode "
                                  f"{None if result is None else result.retcode} "
                                  f"P/L {position.profit:+.2f}"})
                positions = []

            if not positions and in_session:
                tick = mt5.symbol_info_tick(SYMBOL)
                entry = tick.ask
                stop = entry * (1 - STOP_FRACTION)
                request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL, "volume": VOLUME,
                           "type": mt5.ORDER_TYPE_BUY, "price": entry, "sl": stop,
                           "deviation": 30, "magic": MAGIC,
                           "type_filling": mt5.ORDER_FILLING_FOK,
                           "comment": "session drift"}
                result = mt5.order_send(request)
                log({"event": f"OPEN BUY {VOLUME} @ {entry:.2f} SL {stop:.2f} retcode "
                              f"{None if result is None else result.retcode} "
                              f"{'' if result is None else result.comment}"})
        except Exception as exc:  # noqa: BLE001 - a transient terminal error must not stop the loop
            log({"event": f"cycle error {type(exc).__name__}: {exc}"})
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
