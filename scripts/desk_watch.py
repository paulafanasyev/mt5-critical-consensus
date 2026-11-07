"""Watch two chosen tickets and sell them at the peak, then buy the low.

The operator picked tickets 4238 and 4263 (silver shorts) and asked the lead to personal-manage
them: close at the maximum of the move, and buy the minimum. This watcher runs beside the swarm
and does exactly that for those tickets only:

  * it tracks each ticket's own peak profit;
  * it closes the ticket as soon as profit gives back a small slice of that peak — so the exit
    happens near the top, not after the reversal;
  * once the short is banked it places a buy limit at the session low (a little above the recent
    swing low), with the stop under it and a target twice the distance away — the "buy the
    minimum" half of the same swing.

Nothing else in the book is touched.

The tickets this watcher manages live on whatever account `C:\\mt5-exness` is logged into, and that
install belongs to a real broker — so the money can be real money, and this script used to send
without asking anybody. It asks now, at each of its two doors: the stand (`C:\\mt5-fleet\\PAUSE`)
stops every OPENING, demo included, and anything that is not demo needs the same three words as
every other door to real money — `--live`, a registry row calling this login real and enabled, and
the server the terminal named. A real account's pending volume is capped by that row's `max_lots`,
because this file's own default is not a permission. Getting one of these two tickets OUT asks only
that the terminal names the account it is closing: a watcher that refused its own peak exit while the
operator parked the machine would leave the short riding with nothing protecting it, so a run that
starts parked says the refusal in the log and goes on working the exits. And the second half of the
swing — the buy limit — waits for the first half to actually be closed, not merely asked for.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleet import livegate  # noqa: E402

LOG = ROOT / "agent_data" / "desk_watch.jsonl"
STATE = ROOT / "agent_data" / "desk_watch_state.json"
TERMINAL = r"C:\mt5-exness\terminal64.exe"
DESK = 8189999
TICKETS = (11206724238, 11206724263)
DESK_LOTS = 0.01
GIVEBACK_FLOOR_USD = 1.5
GIVEBACK_FRACTION = 0.15
BUY_BELOW_ATR = 0.5


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


def atr(symbol: str) -> float:
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 40)
    if rates is None or len(rates) < 20:
        return 0.0
    closes = [float(r["close"]) for r in rates]
    highs = [float(r["high"]) for r in rates]
    lows = [float(r["low"]) for r in rates]
    ranges = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                  abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))]
    return sum(ranges[-14:]) / 14


def refusal(account, *, live: bool, registry: pathlib.Path | None = None,
            pause_file: pathlib.Path | None = None) -> str:
    """Why this watcher may not open right now; an empty string is the operator's permission.

    The stand stops every OPENING, demo included — it is the operator's hand on the machine, not a
    hint for one process. A demo account is the laboratory this tool was written for, so it may be
    managed as asked. Anything else that sits in this terminal is real money and needs the same three
    words as every other door: `--live`, a registry row naming this login as real and enabled, on the
    server the broker answered. `trade_mode` is read, never assumed: a window that stops describing
    its own money is not proof of play money.

    Exits ask less (`livegate.exit_refusal`): a peak exit that waits for the stand to be lifted is a
    lot left giving its profit back with nobody protecting it.
    """
    if account is None:
        return "терминал не назвал счёт — ни один ордер не отправлен"
    if getattr(account, "trade_mode", None) == mt5.ACCOUNT_TRADE_MODE_DEMO:
        # Play money, and the money this tool was written to manage: only the stand stops it here.
        return livegate.stand_refusal(pause_file)
    return livegate.refusal(getattr(account, "login", None), live=live,
                            server=str(getattr(account, "server", "") or ""),
                            config_path=registry, pause_file=pause_file)


def desk_lots(account, *, registry: pathlib.Path | None = None,
              asked: float = DESK_LOTS) -> float:
    """The volume a pending order may carry, given what the terminal says its account is."""
    if account is None:
        return 0.0
    if getattr(account, "trade_mode", None) == mt5.ACCOUNT_TRADE_MODE_DEMO:
        return asked
    row = livegate.permitted_account(getattr(account, "login", None),
                                     server=str(getattr(account, "server", "") or ""),
                                     config_path=registry)
    return livegate.capped_lots(row, asked)


def send(request: dict, gate) -> dict | None:
    """Every order this watcher writes goes through here, and here it asks permission again."""
    reason = gate()
    if reason:
        log({"event": f"NOT SENT {request.get('comment')}: {reason}"})
        return None
    return mt5.order_send(request)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Деск-надзор за двумя билетами (реальное — "
                                                 "только с --live)")
    parser.add_argument("--live", action="store_true",
                        help="единственный способ разрешить этому запуску тронуть реальные деньги")
    parser.add_argument("--config", default="",
                        help="реестр счетов; по умолчанию config/accounts.yaml")
    parser.add_argument("--interval-sec", type=int, default=15)
    parser.add_argument("--once", action="store_true", help="один проход и выйти")
    args = parser.parse_args(argv)
    registry = pathlib.Path(args.config).resolve() if args.config else None

    if not mt5.initialize(path=TERMINAL):
        raise SystemExit(f"terminal: {mt5.last_error()}")
    account = mt5.account_info()
    if account is not None:
        log({"event": f"счет {account.login}/{account.server} trade_mode "
                      f"{getattr(account, 'trade_mode', None)}"})

    def gate() -> str:
        return refusal(mt5.account_info(), live=args.live, registry=registry)

    def exit_gate() -> str:
        # The peak exit is protection, not a new position: the stand stops the watcher from ADDING
        # risk and must not freeze the exit of risk it already carries. What still has to hold is
        # that the terminal names the account — login and server — whose lot is being closed.
        who = mt5.account_info()
        return livegate.exit_refusal(getattr(who, "login", None),
                                     server=str(getattr(who, "server", "") or ""))

    lots = desk_lots(account, registry=registry)
    reason = gate()
    if reason:
        # Said, and the watcher goes on working: this is the door to OPENING, and the two tickets the
        # operator handed to this process already carry his money. A run that bailed out here used to
        # leave them riding with no peak exit at all — the stand put up after they were opened would
        # have stopped the very thing that protects them. Every send asks again at its own moment.
        log({"event": f"открытий нет: {reason}; свои лоты продолжаю закрывать"})
    if lots <= 0:
        log({"event": f"открытий нет: потолок реестра {lots} лота"})
    state = load_state()
    peaks = state.setdefault("peaks", {})
    bought = state.setdefault("bought", [])
    log({"event": f"WATCH START for {TICKETS}"})

    while True:
        try:
            # The lot is sized at the moment it is sent, from the account the terminal names right
            # now. This process used to read it once at start: a run that began on the demo terminal
            # and was re-logged into a registered real account mid-flight passed the gate — which is
            # re-read every cycle — and then sent the demo volume, the one number in this file that
            # `capped_lots` exists to refuse.
            lots = desk_lots(mt5.account_info(), registry=registry)
            for ticket in TICKETS:
                positions = mt5.positions_get(ticket=ticket) or []
                if not positions:
                    continue
                position = positions[0]
                key = str(ticket)
                peak = max(peaks.get(key, 0.0), position.profit)
                peaks[key] = peak
                floor = peak - max(GIVEBACK_FLOOR_USD, GIVEBACK_FRACTION * peak)
                if peak >= 5.0 and position.profit <= floor:
                    tick = mt5.symbol_info_tick(position.symbol)
                    result = send({
                        "action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol,
                        "volume": position.volume,
                        "type": mt5.ORDER_TYPE_SELL if position.type == 0
                                else mt5.ORDER_TYPE_BUY,
                        "position": position.ticket,
                        "price": tick.bid if position.type == 0 else tick.ask,
                        "deviation": 30, "magic": position.magic,
                        "type_filling": mt5.ORDER_FILLING_FOK,
                        "comment": "desk: peak exit"}, exit_gate)
                    placed = result is not None and result.retcode == 10009
                    log({"event": f"{position.symbol} {ticket}: peak {peak:+.2f} -> "
                                  f"{position.profit:+.2f}, "
                                  f"{'закрыт на вершине' if placed else 'НЕ закрыт'} rtc "
                                  f"{None if result is None else result.retcode}"})
                    if not placed:
                        # The limit below is the second half of a swing whose first half has to be out
                        # of the market. Buying the low beside a short this cycle just admitted it did
                        # not close is a doubled position recorded as a rotation.
                        continue
                    # buy the minimum of the same swing with a limit order
                    sym = position.symbol
                    a = atr(sym)
                    recent = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_H1, 1, 24)
                    if a > 0 and recent is not None and lots > 0:
                        existing = [o for o in (mt5.orders_get() or [])
                                    if o.symbol == sym and o.magic == DESK]
                        if existing:
                            log({"event": f"{sym}: buy-the-low already pending, skip"})
                            continue
                        info = mt5.symbol_info(sym)
                        low = min(float(r["low"]) for r in recent)
                        min_gap = max(info.trade_stops_level * info.point, 0.05 * a)
                        # a buy limit must sit below the market, otherwise the broker rejects it
                        buy_price = min(low + 0.1 * a, tick.bid - min_gap)
                        request = {"action": mt5.TRADE_ACTION_PENDING, "symbol": sym,
                                   "volume": lots, "type": mt5.ORDER_TYPE_BUY_LIMIT,
                                   "price": round(buy_price, 5),
                                   "sl": round(low - 1.5 * a, 5),
                                   "tp": round(buy_price + 3.0 * a, 5),
                                   "magic": DESK, "type_time": mt5.ORDER_TIME_GTC,
                                   "type_filling": mt5.ORDER_FILLING_RETURN,
                                   "comment": "desk: buy the low"}
                        pending = send(request, gate)
                        log({"event": f"{sym}: buy-the-low limit at {buy_price:.5f} "
                                      f"(low {low:.5f}) rtc "
                                      f"{None if pending is None else pending.retcode}"})
                        if pending is not None and pending.retcode == 10009:
                            bought.append(sym)
            save_state(state)
            if args.once:
                return 0
            time.sleep(args.interval_sec)
        except Exception as exc:  # noqa: BLE001 - the watcher must never die
            log({"event": f"cycle error {type(exc).__name__}: {exc}"})
            if args.once:
                return 1
            time.sleep(args.interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
