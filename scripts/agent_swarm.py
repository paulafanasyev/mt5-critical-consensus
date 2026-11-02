#!/usr/bin/env python3
"""A swarm of trading agents on one demo account, one process, many strategies.

Each agent has its own symbol, rule, lot size and magic number, so their positions never
mix. Running them in one process keeps the terminal load sane; separate processes at this
count would fight over the API.

The strategies are the ones the scans actually found, each with its result:

  monday_long         long the pair at the Monday open, close at the Monday close
                      GBPUSD t=4.9, AUDUSD t=4.8, USDCHF t=3.0, EURUSD t=2.5, USDJPY t=2.2
  monday_gap_fill     the weekend gap closes during Monday, with a 0.5% stop
                      basket t=7.4 over 275 trades
  monday_gap_continue gold does the opposite: the gap continues, t=2.4
  first_hour          the first hour of the server day is positive, t=5 to 11

Agents marked experimental have weaker evidence and trade a quarter of the size; they are
there to be measured, not because they are trusted.

Demo only. Run: python scripts/agent_swarm.py
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "agent_data" / "swarm.jsonl"
MAGIC_BASE = 8180000
EXPECTED_LOGIN = 114929502
SERVER_OFFSET = 2
ENTRY_SERVER_HOUR = 0      # Monday 00:00 server = Sunday 22:00 UTC
EXIT_SERVER_HOUR = 21
FIRST_HOUR_UTC = 22        # entry hour for the rollover agents
FIRST_HOUR_EXIT_MINUTE = 55


def agent(name: str, kind: str, symbol: str, lots: float, *, experimental: bool = False,
          stop: float = 0.005) -> dict:
    return {"name": name, "kind": kind, "symbol": symbol, "lots": lots,
            "experimental": experimental, "stop": stop}


AGENTS: list[dict] = [
    # --- Monday long, the strongest calendar effect -------------------------------
    agent("mon-long-gbp", "monday_long", "GBPUSD", 0.05),
    agent("mon-long-aud", "monday_long", "AUDUSD", 0.05),
    agent("mon-long-chf", "monday_long", "USDCHF", 0.05),
    agent("mon-long-eur", "monday_long", "EURUSD", 0.05),
    agent("mon-long-jpy", "monday_long", "USDJPY", 0.05),
    agent("mon-long-us500", "monday_long", "US500", 1.0),
    # --- Monday gap fill -----------------------------------------------------------
    agent("gap-gbp", "monday_gap_fill", "GBPUSD", 0.02),
    agent("gap-chf", "monday_gap_fill", "USDCHF", 0.02),
    agent("gap-aud", "monday_gap_fill", "AUDUSD", 0.02),
    agent("gap-cad", "monday_gap_fill", "USDCAD", 0.02),
    # --- Monday gap continuation on gold ------------------------------------------
    agent("gold-gap", "monday_gap_continue", "XAUUSD", 0.02),
    # --- First hour of the server day ---------------------------------------------
    agent("fh-chf", "first_hour", "USDCHF", 0.02),
    agent("fh-aud", "first_hour", "AUDUSD", 0.02),
    agent("fh-gbp", "first_hour", "GBPUSD", 0.02),
    agent("fh-eur", "first_hour", "EURUSD", 0.02),
    agent("fh-jpy", "first_hour", "USDJPY", 0.02),
    agent("fh-cad", "first_hour", "USDCAD", 0.02),
    # --- daily trend: hold until the 200-day average flips -----------------------
    agent("trend-xau", "daily_trend", "XAUUSD", 0.02),
    agent("trend-xag", "daily_trend", "XAGUSD", 0.01),
    agent("trend-eur", "daily_trend", "EURUSD", 0.02),
    agent("trend-gbp", "daily_trend", "GBPUSD", 0.02),
    agent("trend-jpy", "daily_trend", "USDJPY", 0.02),
    agent("trend-us500", "daily_trend", "US500", 0.1),
    agent("trend-xag-cont", "daily_trend", "XAGUSD", 0.01, experimental=True),
    agent("trend-aud", "daily_trend", "AUDUSD", 0.02),
    agent("trend-cad", "daily_trend", "USDCAD", 0.02),
    agent("trend-nzd", "daily_trend", "NZDUSD", 0.02),
    agent("trend-uki", "daily_trend", "UK100", 0.1),
    agent("trend-jpn", "daily_trend", "JPN225", 1.0),
    agent("trend-us30", "daily_trend", "US30", 0.1),
    # --- experimental, weaker evidence, quarter size ------------------------------
    agent("x-mon-long-xag", "monday_long", "XAGUSD", 0.01, experimental=True),
    agent("x-mon-long-uki", "monday_long", "UK100", 0.1, experimental=True),
    agent("x-mon-long-xau", "monday_long", "XAUUSD", 0.01, experimental=True),
    agent("x-gap-eur", "monday_gap_fill", "EURUSD", 0.01, experimental=True),
    agent("x-gap-jpy", "monday_gap_fill", "USDJPY", 0.01, experimental=True),
    agent("x-gap-us500", "monday_gap_fill", "US500", 0.1, experimental=True),
    agent("x-gap-cont-xag", "monday_gap_continue", "XAGUSD", 0.01, experimental=True),
    agent("x-fh-xau", "first_hour", "XAUUSD", 0.01, experimental=True),
    agent("x-fh-us500", "first_hour", "US500", 0.1, experimental=True),
    agent("x-fh-uki", "first_hour", "UK100", 0.1, experimental=True),
]
for index, item in enumerate(AGENTS):
    item["magic"] = MAGIC_BASE + index


def log(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")
    print(f"[{event['ts'][11:19]}] {event['event']}", flush=True)


def friday_close(symbol: str) -> float | None:
    import MetaTrader5 as mt5

    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 24 * 8)
    if rates is None:
        return None
    for rate in reversed(rates):
        stamp = datetime.fromtimestamp(int(rate["time"]), tz=timezone.utc)
        if (stamp + timedelta(hours=SERVER_OFFSET)).weekday() == 4:
            return float(rate["close"])
    return None


def open_for(mt5, spec: dict) -> dict | None:
    positions = mt5.positions_get(symbol=spec["symbol"]) or []
    for position in positions:
        if position.magic == spec["magic"]:
            return position
    return None


def daily_trend_state(symbol: str) -> str | None:
    """Return BUY if the last daily close is above its 200-day average, SELL if below."""
    import MetaTrader5 as mt5

    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 20000)
    if rates is None or len(rates) < 3000:
        return None
    days: dict[str, float] = {}
    for rate in rates:
        stamp = datetime.fromtimestamp(int(rate["time"]), tz=timezone.utc)
        days[(stamp + timedelta(hours=SERVER_OFFSET)).date().isoformat()] = float(rate["close"])
    closes = [days[key] for key in sorted(days)]
    if len(closes) < 201:
        return None
    weight = 2 / 201
    ema = closes[0]
    for value in closes[1:]:
        ema = value * weight + ema * (1 - weight)
    if closes[-1] > ema * 1.001:
        return "BUY"
    if closes[-1] < ema * 0.999:
        return "SELL"
    return None


SKIP_UNTIL: dict[str, float] = {}


def market_retry_blocked(spec: dict) -> bool:
    """After an 'invalid volume' or 'market closed' answer, wait ten minutes."""
    return SKIP_UNTIL.get(spec["name"], 0) > time.time()


def open_trade(mt5, spec: dict, side: str, tick, *, stop: float | None) -> None:
    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    price = tick.ask if side == "BUY" else tick.bid
    request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": spec["symbol"],
               "volume": spec["lots"], "type": order_type, "price": price,
               "deviation": 30, "magic": spec["magic"],
               "type_filling": mt5.ORDER_FILLING_FOK, "comment": spec["name"][:25]}
    if stop:
        request["sl"] = price * (1 - stop) if side == "BUY" else price * (1 + stop)
    result = mt5.order_send(request)
    if result is not None and result.retcode in (10017, 10018, 10014):
        SKIP_UNTIL[spec["name"]] = time.time() + 600
        log({"event": f"{spec['name']} skipped: retcode {result.retcode} "
                      f"({result.comment}), retry in 10 min"})
        return
    log({"event": f"{spec['name']} OPEN {side} {spec['lots']} {spec['symbol']} @ {price:.5f} "
                  f"retcode {None if result is None else result.retcode} "
                  f"{'EXPERIMENTAL' if spec['experimental'] else ''}"})


def close_trade(mt5, spec: dict, position) -> None:
    tick = mt5.symbol_info_tick(spec["symbol"])
    order_type = mt5.ORDER_TYPE_SELL if position.type == 0 else mt5.ORDER_TYPE_BUY
    price = tick.bid if position.type == 0 else tick.ask
    result = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": spec["symbol"],
                             "volume": position.volume, "type": order_type,
                             "position": position.ticket, "price": price, "deviation": 30,
                             "magic": spec["magic"], "type_filling": mt5.ORDER_FILLING_FOK,
                             "comment": spec["name"][:25]})
    log({"event": f"{spec['name']} CLOSE {spec['symbol']} P/L {position.profit:+.2f} "
                  f"retcode {None if result is None else result.retcode}"})


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
    for spec in AGENTS:
        mt5.symbol_select(spec["symbol"], True)
    log({"event": f"SWARM START: {len(AGENTS)} agents, "
                  f"{sum(1 for a in AGENTS if a['experimental'])} experimental"})
    opened_week: dict[str, str] = {}
    opened_day: dict[str, str] = {}

    while True:
        try:
            now = datetime.now(timezone.utc)
            server = now + timedelta(hours=SERVER_OFFSET)
            for spec in AGENTS:
                if market_retry_blocked(spec):
                    continue
                position = open_for(mt5, spec)
                tick = mt5.symbol_info_tick(spec["symbol"])
                if tick is None or not tick.bid:
                    continue

                if spec["kind"] == "monday_long":
                    week_key = server.strftime("%G-W%V")
                    if (server.weekday() == 0 and server.hour == ENTRY_SERVER_HOUR
                            and position is None and opened_week.get(spec["name"]) != week_key):
                        opened_week[spec["name"]] = week_key
                        open_trade(mt5, spec, "BUY", tick, stop=None)
                    elif position is not None and (server.weekday() != 0
                                                   or server.hour >= EXIT_SERVER_HOUR):
                        close_trade(mt5, spec, position)

                elif spec["kind"] in ("monday_gap_fill", "monday_gap_continue"):
                    week_key = server.strftime("%G-W%V")
                    if (server.weekday() == 0 and server.hour == ENTRY_SERVER_HOUR
                            and position is None and opened_week.get(spec["name"]) != week_key):
                        opened_week[spec["name"]] = week_key
                        reference = friday_close(spec["symbol"])
                        if not reference:
                            continue
                        price = (tick.bid + tick.ask) / 2
                        gap = price / reference - 1
                        if abs(gap) < 0.001:
                            log({"event": f"{spec['name']} gap {gap * 100:+.3f}% below threshold"})
                            continue
                        side = "BUY" if gap < 0 else "SELL"
                        if spec["kind"] == "monday_gap_continue":
                            side = "SELL" if gap < 0 else "BUY"
                        open_trade(mt5, spec, side, tick, stop=spec["stop"])
                    elif position is not None and (server.weekday() != 0
                                                   or server.hour >= EXIT_SERVER_HOUR):
                        close_trade(mt5, spec, position)

                elif spec["kind"] == "daily_trend":
                    state = daily_trend_state(spec["symbol"])
                    if state is None:
                        continue
                    side = "BUY" if state == "BUY" else "SELL"
                    if position is None:
                        open_trade(mt5, spec, side, tick, stop=0.05)
                    elif (position.type == 0 and side == "SELL") or (position.type == 1 and side == "BUY"):
                        close_trade(mt5, spec, position)
                        open_trade(mt5, spec, side, tick, stop=0.05)

                elif spec["kind"] == "first_hour":
                    day_key = now.strftime("%Y-%m-%d")
                    if (now.hour == FIRST_HOUR_UTC and now.minute < 10 and position is None
                            and opened_day.get(spec["name"]) != day_key):
                        opened_day[spec["name"]] = day_key
                        open_trade(mt5, spec, "BUY", tick, stop=None)
                    elif (position is not None and now.hour == FIRST_HOUR_UTC
                          and now.minute >= FIRST_HOUR_EXIT_MINUTE):
                        close_trade(mt5, spec, position)
                    elif position is not None and now.hour == (FIRST_HOUR_UTC + 1) % 24:
                        close_trade(mt5, spec, position)
        except Exception as exc:  # noqa: BLE001
            log({"event": f"cycle error {type(exc).__name__}: {exc}"})
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
