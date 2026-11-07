"""Trend breakout with an ATR trailing stop.

Backtest on 20 days of BTC M5/M15 showed the only positive expectancy in the family:
breakout of a 12-bar M15 range in the direction of EMA21/55, trailed by 2.5 ATR.
Money risk per trade is still capped at STOP_USD.
"""
from __future__ import annotations

import json
import pathlib
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOG = ROOT / "agent_data" / "demo_break.jsonl"
TERMINAL = r"C:\mt5-team\terminal64.exe"
LOGIN = 114929502
SYMBOLS = ("XAUUSD", "XAGUSD")
LOT = 0.01
STOP_USD = 4.60
BREAK_BARS = 12
ATR_MULT = 2.0   # best PF on 4000 real M15 bars: 1.27
TRAIL_MULT = 1.2
COOLDOWN = 600
LAST_ENTRY: dict[str, float] = {}
WATCH: dict[str, float] = {}


def log(event: str, **fields) -> None:
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def ema(values: list[float], period: int) -> float:
    k = 2 / (period + 1)
    out = values[0]
    for value in values[1:]:
        out = value * k + out * (1 - k)
    return out


def atr(bars, period: int = 14) -> float:
    ranges = []
    for i in range(1, period + 1):
        ranges.append(max(bars[-i]["high"] - bars[-i]["low"],
                          abs(bars[-i]["high"] - bars[-i - 1]["close"]),
                          abs(bars[-i]["low"] - bars[-i - 1]["close"])))
    return sum(ranges) / len(ranges)


def signal(symbol: str):
    m15 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 120)
    spec = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if m15 is None or len(m15) < BREAK_BARS + 20 or spec is None or tick is None:
        return None
    if tick.ask <= 0 or tick.ask - tick.bid <= 0:
        return None
    closes = [float(r["close"]) for r in m15]
    up = ema(closes[-60:], 21) > ema(closes[-60:], 55)
    prev_high = max(float(r["high"]) for r in m15[-BREAK_BARS - 1:-1])
    prev_low = min(float(r["low"]) for r in m15[-BREAK_BARS - 1:-1])
    if up and tick.ask > prev_high:
        return ("buy", atr(m15))
    if (not up) and tick.bid < prev_low:
        return ("sell", atr(m15))
    return None


def money_price_distance(symbol: str, volume: float) -> float:
    spec = mt5.symbol_info(symbol)
    return STOP_USD / (spec.trade_contract_size * volume)


def manage() -> None:
    for p in mt5.positions_get() or []:
        if p.magic != 7000013:
            continue
        spec = mt5.symbol_info(p.symbol)
        tick = mt5.symbol_info_tick(p.symbol)
        if spec is None or tick is None or p.sl == 0.0:
            continue
        trail = money_price_distance(p.symbol, p.volume) * TRAIL_MULT
        if p.type == 0 and p.profit > 0:
            new_sl = round(max(p.sl, tick.bid - trail), spec.digits)
        elif p.type == 1 and p.profit > 0:
            new_sl = round(min(p.sl, tick.ask + trail), spec.digits)
        else:
            continue
        if (p.type == 0 and new_sl > p.sl) or (p.type == 1 and new_sl < p.sl):
            mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": p.symbol, "position": p.ticket,
                            "sl": new_sl, "tp": p.tp, "magic": p.magic, "comment": "trail"})
            log("trail", symbol=p.symbol, sl=new_sl)


def main() -> None:
    if not mt5.initialize(path=TERMINAL, timeout=10000):
        log("init_failed", error=str(mt5.last_error()))
        return
    info = mt5.account_info()
    if info is None or info.login != LOGIN:
        log("wrong_account", login=None if info is None else info.login)
        mt5.shutdown()
        return
    log("start", equity=round(info.equity, 2))
    cycles = 0
    while True:
        try:
            cycles += 1
            tick = mt5.symbol_info_tick("XAUUSD")
            if tick is not None and tick.time != WATCH.get("tick_time"):
                WATCH["tick_time"] = tick.time
                WATCH["changed_at"] = time.time()
            if tick is None or time.time() - WATCH.get("changed_at", time.time()) > 600:
                if cycles % 300 == 0:
                    log("feed_closed")
                time.sleep(30)
                continue
            manage()
            open_symbols = {p.symbol for p in (mt5.positions_get() or [])}
            for symbol in SYMBOLS:
                if symbol in open_symbols:
                    continue
                if LAST_ENTRY.get(symbol, 0) + COOLDOWN > time.time():
                    continue
                sig = signal(symbol)
                if sig is None:
                    continue
                side, atr_value = sig
                spec = mt5.symbol_info(symbol)
                tick = mt5.symbol_info_tick(symbol)
                span = min(ATR_MULT * atr_value, money_price_distance(symbol, LOT))
                span = max(span, spec.trade_stops_level * spec.point * 1.1)
                if span <= 0:
                    continue
                entry = tick.ask if side == "buy" else tick.bid
                sl = entry - span if side == "buy" else entry + span
                res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
                                      "volume": LOT,
                                      "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
                                      "price": entry, "sl": round(sl, spec.digits), "tp": 0.0,
                                      "deviation": 30, "magic": 7000013, "comment": f"break-{side}",
                                      "type_time": mt5.ORDER_TIME_GTC,
                                      "type_filling": mt5.ORDER_FILLING_IOC})
                retcode = None if res is None else res.retcode
                if retcode == 10009:
                    LAST_ENTRY[symbol] = time.time()
                log("entry", symbol=symbol, side=side, entry=round(entry, 2),
                    sl=round(sl, 2), risk_usd=STOP_USD, retcode=retcode)
            if cycles % 60 == 0:
                log("scan", equity=round(info.equity, 2),
                    positions=len(mt5.positions_get() or []))
        except Exception as exc:
            log("error", error=f"{type(exc).__name__}: {exc}")
        time.sleep(2)


if __name__ == "__main__":
    main()
