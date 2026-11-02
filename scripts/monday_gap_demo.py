#!/usr/bin/env python3
"""Forward-test the Monday weekend-gap fill on the demo account.

The battery found a consistent effect on three pairs: when Monday opens away from Friday's
close, the day tends to close the gap. Over 3.3 years of hourly bars the result was +0.20%
per trade after spread with t between 3.6 and 4.3, and it held in both halves of the sample.

The rule, exactly as tested:
  - Monday 00:00 server time (22:00 UTC Sunday): compute gap = price / Friday last close - 1
  - if |gap| < 0.10% do nothing
  - otherwise trade toward Friday's close (sell a gap up, buy a gap down), 0.01 lot
  - close at Monday 21:00 UTC (23:00 server), matching the backtest's "Monday close"

One basket per week, three pairs. Demo only.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "agent_data" / "monday_gap.jsonl"
SYMBOLS = ("GBPUSD", "USDCHF", "AUDUSD")
VOLUME = 0.01
MIN_GAP = 0.001
MAGIC = 8172030
EXPECTED_LOGIN = 114929502
# 00:00 server (UTC+2) = 22:00 UTC; the close is 23:00 server = 21:00 UTC
ENTRY_UTC_HOUR = 22
EXIT_UTC_HOUR = 21
SERVER_OFFSET = 2


def log(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")
    print(f"[{event['ts'][11:19]}] {event['event']}", flush=True)


def friday_close(symbol: str) -> float | None:
    import MetaTrader5 as mt5

    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 24 * 8)
    if rates is None or len(rates) == 0:
        return None
    for rate in reversed(rates):
        stamp = datetime.fromtimestamp(int(rate["time"]), tz=timezone.utc)
        server_day = stamp + timedelta(hours=SERVER_OFFSET)
        if server_day.weekday() == 4:  # Friday in server time
            return float(rate["close"])
    return None


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
    for symbol in SYMBOLS:
        mt5.symbol_select(symbol, True)
    log({"event": f"START Monday gap test on {', '.join(SYMBOLS)}"})
    opened_week = None

    while True:
        try:
            now = datetime.now(timezone.utc)
            server_now = now + timedelta(hours=SERVER_OFFSET)
            positions = [p for p in (mt5.positions_get() or []) if p.magic == MAGIC]

            if (server_now.weekday() == 0 and server_now.hour == 0 and opened_week != now.date()
                    and not positions):
                opened_week = now.date()
                for symbol in SYMBOLS:
                    reference = friday_close(symbol)
                    tick = mt5.symbol_info_tick(symbol)
                    if reference is None or not tick or not tick.bid:
                        continue
                    price = (tick.bid + tick.ask) / 2
                    gap = price / reference - 1
                    if abs(gap) < MIN_GAP:
                        log({"event": f"{symbol}: gap {gap * 100:+.3f}% below threshold, skip"})
                        continue
                    trade_type = mt5.ORDER_TYPE_SELL if gap > 0 else mt5.ORDER_TYPE_BUY
                    entry_price = tick.bid if gap > 0 else tick.ask
                    result = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL,
                                             "symbol": symbol, "volume": VOLUME,
                                             "type": trade_type, "price": entry_price,
                                             "deviation": 20, "magic": MAGIC,
                                             "type_filling": mt5.ORDER_FILLING_FOK,
                                             "comment": "monday gap"})
                    log({"event": f"{symbol}: gap {gap * 100:+.3f}% -> "
                                  f"{'SELL' if gap > 0 else 'BUY'} @ {entry_price:.5f} "
                                  f"retcode {None if result is None else result.retcode}"})

            if positions and server_now.hour == 21:
                for position in positions:
                    tick = mt5.symbol_info_tick(position.symbol)
                    result = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL,
                                             "symbol": position.symbol,
                                             "volume": position.volume,
                                             "type": mt5.ORDER_TYPE_BUY if position.type == 1
                                                     else mt5.ORDER_TYPE_SELL,
                                             "position": position.ticket,
                                             "price": tick.ask if position.type == 1 else tick.bid,
                                             "deviation": 20, "magic": MAGIC,
                                             "type_filling": mt5.ORDER_FILLING_FOK,
                                             "comment": "monday gap exit"})
                    log({"event": f"{position.symbol}: CLOSE P/L {position.profit:+.2f} "
                                  f"retcode {None if result is None else result.retcode}"})
        except Exception as exc:  # noqa: BLE001
            log({"event": f"cycle error {type(exc).__name__}: {exc}"})
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
