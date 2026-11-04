"""Mirror the demo book onto the real account while the demo is in profit.

The operator's rule: when the demo is growing, do the same on the real account. This script is the
bridge. It runs two terminal sessions in one process, sequentially:

  1. read the demo account (the laboratory) through its own terminal;
  2. decide, from the demo equity trend and each position's own profit, what deserves to be mirrored;
  3. connect to the separate real terminal and place the same trade with a much smaller size,
     copying the stop and target;
  4. close the real trade as soon as the demo counterpart is gone;
  5. stop everything for the day if the real account loses its daily allowance.

Nothing is sent by default. Every order in this file leaves through `send_order`, and `send_order`
asks `fleet.livegate` at the moment of sending: the stand (`C:\\mt5-fleet\\PAUSE`) must be gone, this
invocation must carry `--live`, and the registry must hold this login as `mode: real` with
`enabled: true`. The registry's own `max_lots` for that row is a hard ceiling on what gets mirrored:
a script that mirrors at its default 0.05 onto an account the operator capped at 0.01 is not
copying the demo, it is multiplying the risk.

It still refuses a terminal that is not logged into a real account — that check guards the money from
a mis-wired path; the checks above guard the money from this script being run at all.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleet import livegate  # noqa: E402

LOG = ROOT / "agent_data" / "real_mirror.jsonl"
STATE = ROOT / "agent_data" / "real_mirror_state.json"
DEMO_MAGIC_FLOOR = 8180000
REAL_MAGIC = 8272026
DEMO_TERMINAL = r"C:\mt5-exness\terminal64.exe"


def log(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(f"[{event['ts'][11:19]}] {event['event']}", flush=True)


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state), encoding="utf-8")


def send_order(mt5, request: dict, gate) -> dict | None:
    """The only door to the real account: ask permission now, not when the loop was entered.

    The stand can appear between the start of a cycle and the moment of a send, so the answer is
    read again here. A refused order returns None and the caller keeps its state unchanged: the
    mirror must not remember a trade it never placed.
    """
    reason = gate()
    if reason:
        log({"event": f"NOT SENT {request.get('action')} {request.get('symbol')} "
                      f"{request.get('volume')}: {reason}"})
        return None
    return mt5.order_send(request)


def hourly_uptrend(mt5, symbol: str) -> bool:
    """Price above EMA20 and EMA20 above EMA50 on H1 — only buy what is really rising."""
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 60)
    if rates is None or len(rates) < 50:
        return False
    closes = [float(rate["close"]) for rate in rates]

    def ema(values, period):
        weight = 2 / (period + 1)
        result = values[0]
        for value in values[1:]:
            result = value * weight + result * (1 - weight)
        return result

    fast, slow = ema(closes, 20), ema(closes, 50)
    return closes[-1] > fast > slow


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
    parser.add_argument("--live", action="store_true",
                        help="единственный способ разрешить этому запуску тронуть реальные деньги")
    parser.add_argument("--config", default="",
                        help="реестр счетов; по умолчанию config/accounts.yaml")
    parser.add_argument("--interval-sec", type=int, default=30)
    parser.add_argument("--real-lots", type=float, default=0.01)
    parser.add_argument("--daily-stop-pct", type=float, default=0.5)
    parser.add_argument("--min-position-profit", type=float, default=0.0,
                        help="mirror only positions at least this much in profit")
    parser.add_argument("--real-tp-usd", type=float, default=3.0,
                        help="bank the real copy at this profit, earlier than the demo")
    parser.add_argument("--max-hold-min", type=int, default=90,
                        help="close the real copy after this many minutes, earlier than the demo")
    parser.add_argument("--once", action="store_true", help="один цикл и выйти")
    args = parser.parse_args(argv)
    registry = pathlib.Path(args.config).resolve() if args.config else None

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
            if not mt5.initialize(path=DEMO_TERMINAL):
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
                if args.once:
                    return 1
                time.sleep(args.interval_sec)
                continue
            if real_account.trade_mode != 2:
                log({"event": f"refusing: {real_account.login} is not a real account"})
                mt5.shutdown()
                if args.once:
                    return 1
                time.sleep(args.interval_sec)
                continue

            # --- 2b. the operator's permission, asked for this account and read again per send ---
            def gate() -> str:
                return livegate.refusal(real_account.login, live=args.live,
                                        server=str(real_account.server or ""),
                                        config_path=registry)

            reason = gate()
            if reason:
                log({"event": f"стоп, ничего не отправлено: {reason}"})
                mt5.shutdown()
                return 1
            row = livegate.permitted_account(real_account.login,
                                             server=str(real_account.server or ""),
                                             config_path=registry)
            lots = livegate.capped_lots(row, args.real_lots)
            if lots < args.real_lots:
                log({"event": f"лот срезан по реестру: {args.real_lots} → {lots} "
                              f"(max_lots записи {row.name})"})
            if state.get("day_start_equity") is None:
                state["day_start_equity"] = real_account.equity
                save_state(state)
            limit = state["day_start_equity"] * (1 - args.daily_stop_pct / 100)
            if real_account.equity < limit and not state.get("halted"):
                for position in [p for p in (mt5.positions_get() or [])
                                 if p.magic == REAL_MAGIC]:
                    tick = mt5.symbol_info_tick(position.symbol)
                    send_order(mt5, {"action": mt5.TRADE_ACTION_DEAL,
                                     "symbol": position.symbol, "volume": position.volume,
                                     "type": mt5.ORDER_TYPE_SELL if position.type == 0
                                             else mt5.ORDER_TYPE_BUY,
                                     "position": position.ticket,
                                     "price": tick.bid if position.type == 0 else tick.ask,
                                     "deviation": 30, "magic": REAL_MAGIC,
                                     "type_filling": mt5.ORDER_FILLING_FOK,
                                     "comment": "daily stop"}, gate)
                state["halted"] = True
                save_state(state)
                log({"event": f"DAILY STOP: real equity {real_account.equity:.2f} below "
                              f"{limit:.2f}, всё своё закрыто"})

            real_positions = [p for p in (mt5.positions_get() or []) if p.magic == REAL_MAGIC]
            other = len(mt5.positions_get() or []) - len(real_positions)
            if other:
                log({"event": f"{other} лот(ов) на реальном счёте не наши (magic != {REAL_MAGIC}): "
                              f"не тронуты"})
            mirrored = {position.symbol: position for position in real_positions}

            if not state.get("halted"):
                # --- 3. close what the demo has already closed ---
                open_demo_symbols = {item["symbol"] for item in demo_positions}
                for symbol, position in list(mirrored.items()):
                    if symbol in open_demo_symbols:
                        continue
                    tick = mt5.symbol_info_tick(symbol)
                    result = send_order(mt5, {
                        "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
                        "volume": position.volume,
                        "type": mt5.ORDER_TYPE_SELL if position.type == 0
                                else mt5.ORDER_TYPE_BUY,
                        "position": position.ticket,
                        "price": tick.bid if position.type == 0 else tick.ask,
                        "deviation": 30, "magic": REAL_MAGIC,
                        "type_filling": mt5.ORDER_FILLING_FOK,
                        "comment": "demo closed"}, gate)
                    log({"event": f"{symbol}: демо закрыл, реальная копия "
                                  f"{'закрыта' if result else 'НЕ закрыта'} rtc "
                                  f"{None if result is None else result.retcode}"})

                # --- 4. mirror what is growing ---
                if growth:
                    for item in demo_positions:
                        if item["symbol"] in mirrored or item["profit"] < args.min_position_profit:
                            continue
                        if item["type"] == 1 or not hourly_uptrend(mt5, item["symbol"]):
                            log({"event": f"{item['symbol']}: не зеркалировано — реальный счёт "
                                          f"покупает только то, что действительно растёт"})
                            continue
                        tick = mt5.symbol_info_tick(item["symbol"])
                        if not tick:
                            continue
                        side = mt5.ORDER_TYPE_BUY if item["type"] == 0 else mt5.ORDER_TYPE_SELL
                        price = tick.ask if item["type"] == 0 else tick.bid
                        request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": item["symbol"],
                                   "volume": lots, "type": side, "price": price,
                                   "deviation": 30, "magic": REAL_MAGIC,
                                   "type_filling": mt5.ORDER_FILLING_FOK,
                                   "comment": "mirror demo"}
                        if item["sl"]:
                            request["sl"] = item["sl"]
                        if item["tp"]:
                            request["tp"] = item["tp"]
                        result = send_order(mt5, request, gate)
                        if result is None:
                            continue
                        log({"event": f"{item['symbol']}: зеркалит демо билет {item['ticket']} "
                                      f"по {price:.5f} лот {lots} rtc {result.retcode}"})
            # sell a little earlier than the demo: own small target, own time limit
            opened = state.setdefault("real_opened", {})
            for position in real_positions:
                key = str(position.ticket)
                opened.setdefault(key, time.time())
                age_min = (time.time() - opened[key]) / 60
                if position.profit >= args.real_tp_usd or age_min >= args.max_hold_min:
                    tick = mt5.symbol_info_tick(position.symbol)
                    result = send_order(mt5, {
                        "action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol,
                        "volume": position.volume,
                        "type": mt5.ORDER_TYPE_SELL if position.type == 0
                                else mt5.ORDER_TYPE_BUY,
                        "position": position.ticket,
                        "price": tick.bid if position.type == 0 else tick.ask,
                        "deviation": 30, "magic": REAL_MAGIC,
                        "type_filling": mt5.ORDER_FILLING_FOK,
                        "comment": "real early exit"}, gate)
                    if result is not None:
                        opened.pop(key, None)
                        log({"event": f"{position.symbol}: копия зафиксирована раньше демо на "
                                      f"{position.profit:+.2f} после {age_min:.0f} мин, rtc "
                                      f"{result.retcode}"})
            state["real_opened"] = opened
            save_state(state)
            mt5.shutdown()
            log({"event": f"HEARTBEAT demo_trend {trend:+.2f} growth {growth} лот {lots} "
                          f"real_equity {real_account.equity:.2f} mirrored {len(mirrored)}"})
            if args.once:
                return 0
        except Exception as exc:  # noqa: BLE001 - the bridge must never die
            log({"event": f"cycle error {type(exc).__name__}: {exc}"})
            if args.once:
                return 1
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
