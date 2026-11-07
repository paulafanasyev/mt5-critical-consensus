#!/usr/bin/env python3
"""Forward-test the rollover recovery on the demo account.

The intraday scan found that USD pairs rise during the first hour of the server day
(00:00 server time = 22:00 UTC), by about +0.03-0.04% net of the quoted spread, with a
t-statistic above 10 over ~670 days. That may be a real rollover effect or a quoting
artefact of this server. The only way to tell is to trade it with real fills:

  - at 22:00 UTC, buy 0.01 lot of USDCHF (the strongest of the five pairs),
  - at 22:55 UTC, close it,
  - record the actual entry and exit prices and the result.

One micro trade per day. Demo only; read the account mode before every order.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "agent_data" / "rollover_test.jsonl"
SYMBOL = "USDCHF"
VOLUME = 0.01
ENTRY_MINUTE = 0      # 22:00 UTC
EXIT_MINUTE = 55      # 22:55 UTC
MAGIC = 8172029
EXPECTED_LOGIN = 114929502


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
        raise SystemExit(f"unexpected account {account.login if account else None}")
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        raise SystemExit("refusing a non-demo account")
    mt5.symbol_select(SYMBOL, True)
    log({"event": f"START rollover test: buy {VOLUME} {SYMBOL} at 22:00 UTC, close at 22:55"})

    opened_today = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            positions = [p for p in (mt5.positions_get(symbol=SYMBOL) or []) if p.magic == MAGIC]
            tick = mt5.symbol_info_tick(SYMBOL)

            if (not positions and now.hour == 22 and now.minute >= ENTRY_MINUTE
                    and now.minute < ENTRY_MINUTE + 5 and opened_today != now.date()):
                result = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL,
                                         "volume": VOLUME, "type": mt5.ORDER_TYPE_BUY,
                                         "price": tick.ask, "deviation": 20, "magic": MAGIC,
                                         "type_filling": mt5.ORDER_FILLING_FOK,
                                         "comment": "rollover test"})
                opened_today = now.date()
                log({"event": f"OPEN BUY {VOLUME} bid {tick.bid:.5f} ask {tick.ask:.5f} "
                              f"retcode {None if result is None else result.retcode} "
                              f"{'' if result is None else result.comment}"})

            if positions and now.hour == 22 and now.minute >= EXIT_MINUTE:
                for position in positions:
                    result = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL,
                                             "volume": position.volume,
                                             "type": mt5.ORDER_TYPE_SELL,
                                             "position": position.ticket, "price": tick.bid,
                                             "deviation": 20, "magic": MAGIC,
                                             "type_filling": mt5.ORDER_FILLING_FOK,
                                             "comment": "rollover test exit"})
                    log({"event": f"CLOSE bid {tick.bid:.5f} ask {tick.ask:.5f} P/L "
                                  f"{position.profit:+.2f} retcode "
                                  f"{None if result is None else result.retcode}"})
        except Exception as exc:  # noqa: BLE001
            log({"event": f"cycle error {type(exc).__name__}: {exc}"})
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
