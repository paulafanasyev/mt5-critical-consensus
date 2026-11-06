"""Demo-only laboratory: buy cheap, sell expensive on M1/M5, many trades, no real money.

The operator wants proof on the demo account first. One position per symbol (the account may be
netting), stop $5 of movement, target 1.5R, and a hard refusal if the API is not the demo login.
"""
from __future__ import annotations

import json
import pathlib
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

ROOT = pathlib.Path(__file__).resolve().parents[1]
EVENTS = ROOT / "agent_data" / "demo_lab.jsonl"
TERMINAL = r"C:\mt5-team\terminal64.exe"
DEMO_LOGIN = 114929502
SYMBOLS = ("XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY")
STOP_MONEY = 5.00
RR = 1.5


def log(event: str, **fields) -> None:
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
    with EVENTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def ema(values, period):
    k = 2 / (period + 1)
    out = values[0]
    for value in values[1:]:
        out = value * k + out * (1 - k)
    return out


def span_for(symbol: str, money: float) -> float:
    spec = mt5.symbol_info(symbol)
    if spec is None or spec.trade_tick_value <= 0 or spec.trade_tick_size <= 0:
        return 0.0
    return money / (spec.trade_tick_value * 0.01 / spec.trade_tick_size)


PEAKS: dict[int, float] = {}


def manage_demo() -> None:
    """Close at the maximum: bank when profit retreats from its own peak, or the M1 turns."""
    for position in mt5.positions_get() or []:
        if position.magic != 7000011:
            continue
        key = position.ticket
        peak = max(PEAKS.get(key, position.profit), position.profit)
        PEAKS[key] = peak
        m1 = mt5.copy_rates_from_pos(position.symbol, mt5.TIMEFRAME_M1, 0, 30)
        turning = False
        if m1 is not None and len(m1) >= 3:
            closes = [float(r["close"]) for r in m1]
            turning = (position.type == 0 and closes[-1] < closes[-2] < closes[-3]) or \
                      (position.type == 1 and closes[-1] > closes[-2] > closes[-3])
        giveback = max(0.40, 0.25 * peak)
        if (peak >= 0.80 and position.profit <= peak - giveback) or (turning and position.profit > 0.30):
            tick = mt5.symbol_info_tick(position.symbol)
            price = tick.bid if position.type == 0 else tick.ask
            typ = mt5.ORDER_TYPE_SELL if position.type == 0 else mt5.ORDER_TYPE_BUY
            res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol,
                                  "volume": position.volume, "type": typ, "price": price,
                                  "position": position.ticket, "deviation": 30, "magic": 7000011,
                                  "comment": "peak-exit", "type_time": mt5.ORDER_TIME_GTC,
                                  "type_filling": mt5.ORDER_FILLING_IOC})
            retcode = None if res is None else res.retcode
            if retcode == 10030:  # invalid order on this feed: retry with FOK
                res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol,
                                      "volume": position.volume, "type": typ, "price": price,
                                      "position": position.ticket, "deviation": 30, "magic": 7000011,
                                      "comment": "peak-exit", "type_time": mt5.ORDER_TIME_GTC,
                                      "type_filling": mt5.ORDER_FILLING_FOK})
                retcode = None if res is None else res.retcode
            log("peak_exit", symbol=position.symbol, peak=round(peak, 2),
                profit=round(position.profit, 2), retcode=retcode)
            PEAKS.pop(key, None)


def main() -> None:
    if not mt5.initialize(path=TERMINAL, timeout=10000):
        log("init_failed", error=str(mt5.last_error()))
        return
    info = mt5.account_info()
    if info is None or info.login != DEMO_LOGIN:
        log("wrong_account", login=None if info is None else info.login)
        mt5.shutdown()
        return
    log("start", equity=round(info.equity, 2))
    cycles = 0
    last_entry: dict[str, float] = {}
    while True:
        try:
            info = mt5.account_info()
            if info is None or info.login != DEMO_LOGIN:
                log("wrong_account", login=None if info is None else info.login)
                mt5.shutdown()
                time.sleep(20)
                mt5.initialize(path=TERMINAL, timeout=10000)
                continue
            manage_demo()
            open_symbols = {p.symbol for p in (mt5.positions_get() or [])}
            for symbol in SYMBOLS:
                if symbol in open_symbols:
                    continue
                spec = mt5.symbol_info(symbol)
                m5 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 200)
                m1 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 120)
                if spec is None or m5 is None or m1 is None or len(m5) < 60 or len(m1) < 40:
                    continue
                c5 = [float(r["close"]) for r in m5]
                c1 = [float(r["close"]) for r in m1]
                trend5 = ema(c5, 21) - ema(c5, 55)
                low24 = min(float(r["low"]) for r in m5[-24:])
                high24 = max(float(r["high"]) for r in m5[-24:])
                spans = [max(float(m5[i]["high"]) - float(m5[i]["low"]),
                             abs(float(m5[i]["high"]) - float(m5[i - 1]["close"])),
                             abs(float(m5[i]["low"]) - float(m5[i - 1]["close"]))) for i in range(1, 15)]
                atr5 = sum(spans) / len(spans)
                tick = mt5.symbol_info_tick(symbol)
                if tick is None or atr5 <= 0:
                    continue
                spread = spec.ask - spec.bid
                # edge entry plus a momentum entry, so the lab trades often enough to be measured
                edge_cheap = (tick.ask - low24) <= 1.5 * atr5
                edge_dear = (high24 - tick.bid) <= 1.5 * atr5
                mom_up = c1[-1] > c1[-2] > c1[-3]
                mom_down = c1[-1] < c1[-2] < c1[-3]
                h12 = max(float(r["high"]) for r in m1[-13:-1])
                l12 = min(float(r["low"]) for r in m1[-13:-1])
                now = time.time()
                if now - last_entry.get(symbol, 0) < 45:
                    continue
                # frequent: a confirmed M1 impulse is enough, edge is a bonus
                cheap = mom_up and (edge_cheap or c1[-1] >= h12 or trend5 > 0)
                expensive = mom_down and (edge_dear or c1[-1] <= l12 or trend5 < 0)
                if not (cheap or expensive):
                    continue
                side = "buy" if cheap else "sell"
                distance = max(span_for(symbol, STOP_MONEY), spec.trade_stops_level * spec.point * 1.2,
                               spread * 3)
                if spread <= 0:
                    continue  # demo feed has not ticked this symbol yet
                if spread > 0.2 * distance:
                    continue
                entry = tick.ask if side == "buy" else tick.bid
                sl = entry - distance if side == "buy" else entry + distance
                tp = entry + RR * distance if side == "buy" else entry - RR * distance
                res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
                                      "volume": 0.01,
                                      "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
                                      "price": entry, "sl": round(sl, spec.digits),
                                      "tp": round(tp, spec.digits), "deviation": 25,
                                      "magic": 7000011, "comment": f"lab-{side}",
                                      "type_time": mt5.ORDER_TIME_GTC,
                                      "type_filling": mt5.ORDER_FILLING_IOC})
                retcode = None if res is None else res.retcode
                if retcode == 10030:  # invalid order: retry once with FOK filling
                    res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
                                          "volume": 0.01,
                                          "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
                                          "price": entry, "sl": round(sl, spec.digits),
                                          "tp": round(tp, spec.digits), "deviation": 25,
                                          "magic": 7000011, "comment": f"lab-{side}",
                                          "type_time": mt5.ORDER_TIME_GTC,
                                          "type_filling": mt5.ORDER_FILLING_FOK})
                    retcode = None if res is None else res.retcode
                last_entry[symbol] = time.time()
                log("entry", symbol=symbol, side=side, sl=round(sl, spec.digits),
                    tp=round(tp, spec.digits), spread=round(spread, spec.digits),
                    retcode=retcode)
            cycles += 1
            if cycles % 12 == 0:
                log("scan", equity=round(info.equity, 2), open_positions=len(mt5.positions_get() or []))
        except Exception as exc:  # noqa: BLE001 - the lab must survive bad ticks
            log("error", error=f"{type(exc).__name__}: {exc}")
        time.sleep(2)


if __name__ == "__main__":
    main()
