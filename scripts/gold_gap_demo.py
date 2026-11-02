#!/usr/bin/env python3
"""Agent: gold gap continuation on Monday.

On the terminal's data the FX pairs fill their Monday gap, but gold does the opposite:
when Monday opens away from Friday's close, the move continues during the day. Over 80
Mondays the continuation was +0.325% per trade after spread (t = 2.40), positive in both
halves of the sample (+0.357% and +0.292%).

Rule: at Monday 00:00 server time, if |gap| > 0.1%, trade in the direction of the gap,
0.01 lots, 0.5% stop, close at Monday 21:00 UTC.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "agent_data" / "gold_gap.jsonl"
SYMBOL = "XAUUSD"
VOLUME = 0.01
MIN_GAP = 0.001
STOP = 0.005
MAGIC = 8172032
EXPECTED_LOGIN = 114929502
SERVER_OFFSET = 2


def log(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")
    print(f"[{event['ts'][11:19]}] {event['event']}", flush=True)


def friday_close() -> float | None:
    import MetaTrader5 as mt5

    rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 1, 24 * 8)
    if rates is None:
        return None
    for rate in reversed(rates):
        stamp = datetime.fromtimestamp(int(rate["time"]), tz=timezone.utc)
        if (stamp + timedelta(hours=SERVER_OFFSET)).weekday() == 4:
            return float(rate["close"])
    return None


def main(argv: list[str] | None = None) -> int:
    import MetaTrader5 as mt5

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-sec", type=int, default=60)
    args = parser.parse_args(argv)
    if not mt5.initialize():
        raise SystemExit("MetaTrader5.initialize failed")
    account = mt5.account_info()
    if account is None or account.login != EXPECTED_LOGIN:
        raise SystemExit("unexpected account")
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        raise SystemExit("refusing a non-demo account")
    mt5.symbol_select(SYMBOL, True)
    log({"event": "START gold gap continuation agent"})
    opened_week = None

    while True:
        try:
            now = datetime.now(timezone.utc)
            server = now + timedelta(hours=SERVER_OFFSET)
            positions = [p for p in (mt5.positions_get(symbol=SYMBOL) or []) if p.magic == MAGIC]

            if (server.weekday() == 0 and server.hour == 0 and opened_week != now.date()
                    and not positions):
                opened_week = now.date()
                reference = friday_close()
                tick = mt5.symbol_info_tick(SYMBOL)
                if reference and tick and tick.bid:
                    price = (tick.bid + tick.ask) / 2
                    gap = price / reference - 1
                    if abs(gap) >= MIN_GAP:
                        trade_type = mt5.ORDER_TYPE_BUY if gap > 0 else mt5.ORDER_TYPE_SELL
                        entry = tick.ask if gap > 0 else tick.bid
                        stop = entry * (1 - STOP) if gap > 0 else entry * (1 + STOP)
                        result = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL,
                                                 "symbol": SYMBOL, "volume": VOLUME,
                                                 "type": trade_type, "price": entry,
                                                 "sl": stop, "deviation": 30, "magic": MAGIC,
                                                 "type_filling": mt5.ORDER_FILLING_FOK,
                                                 "comment": "gold gap continuation"})
                        log({"event": f"gap {gap * 100:+.3f}% -> "
                                      f"{'BUY' if gap > 0 else 'SELL'} @ {entry:.2f} "
                                      f"SL {stop:.2f} retcode "
                                      f"{None if result is None else result.retcode}"})
                    else:
                        log({"event": f"gap {gap * 100:+.3f}% below threshold"})

            if positions and server.hour >= 21:
                for position in positions:
                    tick = mt5.symbol_info_tick(SYMBOL)
                    result = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL,
                                             "symbol": SYMBOL, "volume": position.volume,
                                             "type": mt5.ORDER_TYPE_SELL
                                                     if position.type == 0 else mt5.ORDER_TYPE_BUY,
                                             "position": position.ticket,
                                             "price": tick.bid if position.type == 0 else tick.ask,
                                             "deviation": 30, "magic": MAGIC,
                                             "type_filling": mt5.ORDER_FILLING_FOK,
                                             "comment": "gold gap exit"})
                    log({"event": f"CLOSE P/L {position.profit:+.2f} retcode "
                                  f"{None if result is None else result.retcode}"})
        except Exception as exc:  # noqa: BLE001
            log({"event": f"cycle error {type(exc).__name__}: {exc}"})
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
