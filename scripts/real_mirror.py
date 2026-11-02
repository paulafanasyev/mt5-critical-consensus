"""Mirror the demo book onto the real account while the demo is in profit.

The operator's rule: when the demo is growing, do the same on the real account. This script
is the bridge. It runs two terminal sessions in one process, sequentially:

  1. read the demo account (the laboratory) through its own terminal;
  2. decide, from the demo equity trend and each position's own profit, what deserves to be
     mirrored;
  3. connect to the separate real terminal and place the same trade with a much smaller
     size, copying the stop and target;
  4. close the real trade as soon as the demo counterpart is gone;
  5. stop everything for the day if the real account loses its daily allowance.

It refuses to run unless the second terminal is logged into a real account.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "agent_data" / "real_mirror.jsonl"
STATE = ROOT / "agent_data" / "real_mirror_state.json"
DEMO_MAGIC_FLOOR = 8180000
REAL_MAGIC = 8272026


def log(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")
    print(f"[{event['ts'][11:19]}] {event['event']}", flush=True)


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state), encoding="utf-8")


def demo_equity_trend(points: int = 6) -> float:
    """Change in demo equity over the last few supervisor heartbeats."""
    path = ROOT / "agent_data" / "supervisor.jsonl"
    if not path.exists():
        return 0.0
    equities = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = event.get("event", "")
        if not text.startswith("HEARTBEAT"):
            continue
        parts = text.split()
        for index, word in enumerate(parts):
            if word == "equity" and index + 1 < len(parts):
                try:
                    equities.append(float(parts[index + 1]))
                except ValueError:
                    pass
    if len(equities) < points:
        return 0.0
    return equities[-1] - equities[-points]


def main(argv: list[str] | None = None) -> int:
    import MetaTrader5 as mt5

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-path", required=True,
                        help="path to terminal64.exe of the real-account instance")
    parser.add_argument("--interval-sec", type=int, default=30)
    parser.add_argument("--real-lots", type=float, default=0.01)
    parser.add_argument("--daily-stop-pct", type=float, default=0.5)
    parser.add_argument("--min-position-profit", type=float, default=0.0,
                        help="mirror only positions at least this much in profit")
    args = parser.parse_args(argv)

    state = load_state()
    daily_key = datetime.now(timezone.utc).date().isoformat()
    if state.get("day") != daily_key:
        state["day"] = daily_key
        state["day_start_equity"] = None
        state["halted"] = False
        save_state(state)

    log({"event": "MIRROR START: demo -> real, growth only"})

    while True:
        try:
            # --- 1. the demo laboratory ---
            if not mt5.initialize():
                log({"event": "demo terminal unavailable, retry"})
                time.sleep(args.interval_sec)
                continue
            demo_positions = [p for p in (mt5.positions_get() or [])
                              if p.magic >= DEMO_MAGIC_FLOOR]
            demo_positions = [{"ticket": p.ticket, "symbol": p.symbol, "type": p.type,
                               "volume": p.volume, "price_open": p.price_open,
                               "sl": p.sl, "tp": p.tp, "profit": p.profit}
                              for p in demo_positions]
            mt5.shutdown()
            trend = demo_equity_trend()
            growth = trend > 0

            # --- 2. the real terminal ---
            if not mt5.initialize(path=args.real_path):
                log({"event": "real terminal unavailable, retry"})
                time.sleep(args.interval_sec)
                continue
            real_account = mt5.account_info()
            if real_account is None:
                log({"event": "real terminal not logged in"})
                mt5.shutdown()
                time.sleep(args.interval_sec)
                continue
            if real_account.trade_mode != 2:
                log({"event": f"refusing: {real_account.login} is not a real account"})
                mt5.shutdown()
                time.sleep(args.interval_sec)
                continue
            if state.get("day_start_equity") is None:
                state["day_start_equity"] = real_account.equity
                save_state(state)
            limit = state["day_start_equity"] * (1 - args.daily_stop_pct / 100)
            if real_account.equity < limit and not state.get("halted"):
                for position in [p for p in (mt5.positions_get() or [])
                                 if p.magic == REAL_MAGIC]:
                    tick = mt5.symbol_info_tick(position.symbol)
                    mt5.order_send({"action": mt5.TRADE_ACTION_DEAL,
                                    "symbol": position.symbol, "volume": position.volume,
                                    "type": mt5.ORDER_TYPE_SELL if position.type == 0
                                            else mt5.ORDER_TYPE_BUY,
                                    "position": position.ticket,
                                    "price": tick.bid if position.type == 0 else tick.ask,
                                    "deviation": 30, "magic": REAL_MAGIC,
                                    "type_filling": mt5.ORDER_FILLING_FOK,
                                    "comment": "daily stop"})
                state["halted"] = True
                save_state(state)
                log({"event": f"DAILY STOP: real equity {real_account.equity:.2f} below "
                              f"{limit:.2f}, everything closed"})

            real_positions = [p for p in (mt5.positions_get() or []) if p.magic == REAL_MAGIC]
            mirrored = {position.symbol: position for position in real_positions}

            if not state.get("halted"):
                # --- 3. close what the demo has already closed ---
                open_demo_symbols = {item["symbol"] for item in demo_positions}
                for symbol, position in list(mirrored.items()):
                    if symbol in open_demo_symbols:
                        continue
                    tick = mt5.symbol_info_tick(symbol)
                    result = mt5.order_send({
                        "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
                        "volume": position.volume,
                        "type": mt5.ORDER_TYPE_SELL if position.type == 0
                                else mt5.ORDER_TYPE_BUY,
                        "position": position.ticket,
                        "price": tick.bid if position.type == 0 else tick.ask,
                        "deviation": 30, "magic": REAL_MAGIC,
                        "type_filling": mt5.ORDER_FILLING_FOK,
                        "comment": "demo closed"})
                    log({"event": f"{symbol}: demo closed it, real closed too rtc "
                                  f"{None if result is None else result.retcode}"})

                # --- 4. mirror what is growing ---
                if growth:
                    for item in demo_positions:
                        if item["symbol"] in mirrored or item["profit"] < args.min_position_profit:
                            continue
                        tick = mt5.symbol_info_tick(item["symbol"])
                        if not tick:
                            continue
                        side = mt5.ORDER_TYPE_BUY if item["type"] == 0 else mt5.ORDER_TYPE_SELL
                        price = tick.ask if item["type"] == 0 else tick.bid
                        request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": item["symbol"],
                                   "volume": args.real_lots, "type": side, "price": price,
                                   "deviation": 30, "magic": REAL_MAGIC,
                                   "type_filling": mt5.ORDER_FILLING_FOK,
                                   "comment": "mirror demo"}
                        if item["sl"]:
                            request["sl"] = item["sl"]
                        if item["tp"]:
                            request["tp"] = item["tp"]
                        result = mt5.order_send(request)
                        log({"event": f"{item['symbol']}: mirrored demo ticket "
                                      f"{item['ticket']} at {price:.5f} rtc "
                                      f"{None if result is None else result.retcode}"})
            mt5.shutdown()
            log({"event": f"HEARTBEAT demo_trend {trend:+.2f} growth {growth} "
                          f"real_equity {real_account.equity:.2f} mirrored {len(mirrored)}"})
        except Exception as exc:  # noqa: BLE001 - the bridge must never die
            log({"event": f"cycle error {type(exc).__name__}: {exc}"})
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
