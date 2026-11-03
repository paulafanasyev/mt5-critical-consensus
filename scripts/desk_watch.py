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
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "agent_data" / "desk_watch.jsonl"
STATE = ROOT / "agent_data" / "desk_watch_state.json"
TERMINAL = r"C:\mt5-exness\terminal64.exe"
DESK = 8189999
TICKETS = (11206724238, 11206724263)
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


def main() -> int:
    if not mt5.initialize(path=TERMINAL):
        raise SystemExit(f"terminal: {mt5.last_error()}")
    state = load_state()
    peaks = state.setdefault("peaks", {})
    bought = state.setdefault("bought", [])
    log({"event": f"WATCH START for {TICKETS}"})

    while True:
        try:
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
                    result = mt5.order_send({
                        "action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol,
                        "volume": position.volume,
                        "type": mt5.ORDER_TYPE_SELL if position.type == 0
                                else mt5.ORDER_TYPE_BUY,
                        "position": position.ticket,
                        "price": tick.bid if position.type == 0 else tick.ask,
                        "deviation": 30, "magic": position.magic,
                        "type_filling": mt5.ORDER_FILLING_FOK,
                        "comment": "desk: peak exit"})
                    log({"event": f"{position.symbol} {ticket}: peak {peak:+.2f} -> "
                                  f"{position.profit:+.2f}, closed at the top rtc "
                                  f"{None if result is None else result.retcode}"})
                    # buy the minimum of the same swing with a limit order
                    sym = position.symbol
                    a = atr(sym)
                    recent = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_H1, 1, 24)
                    if a > 0 and recent is not None:
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
                                   "volume": 0.01, "type": mt5.ORDER_TYPE_BUY_LIMIT,
                                   "price": round(buy_price, 5),
                                   "sl": round(low - 1.5 * a, 5),
                                   "tp": round(buy_price + 3.0 * a, 5),
                                   "magic": DESK, "type_time": mt5.ORDER_TIME_GTC,
                                   "type_filling": mt5.ORDER_FILLING_RETURN,
                                   "comment": "desk: buy the low"}
                        pending = mt5.order_send(request)
                        log({"event": f"{sym}: buy-the-low limit at {buy_price:.5f} "
                                      f"(low {low:.5f}) rtc "
                                      f"{None if pending is None else pending.retcode}"})
                        if pending is not None and pending.retcode == 10009:
                            bought.append(sym)
            save_state(state)
            time.sleep(15)
        except Exception as exc:  # noqa: BLE001 - the watcher must never die
            log({"event": f"cycle error {type(exc).__name__}: {exc}"})
            time.sleep(15)


if __name__ == "__main__":
    raise SystemExit(main())
