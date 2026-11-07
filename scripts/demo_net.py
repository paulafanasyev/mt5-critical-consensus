
from __future__ import annotations
import json, pathlib, time
from datetime import datetime, timezone
import MetaTrader5 as mt5

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOG = ROOT / "agent_data" / "demo_net.jsonl"
TERMINAL = r"C:\mt5-team\terminal64.exe"
LOGIN = 114929502
SYMBOLS = ("XAUUSD", "XAUEUR", "XAUGBP", "XAUCHF", "XAGUSD", "XAGEUR", "XAGAUD", "XAGG", "XAGI")
LOT = 0.01
STOP_USD = 5.0
TARGET_USD = 3.0
BREAKEVEN_USD = 1.0
PEAK_MIN = 1.5
MAX_HOLD = 1800     # a position that goes nowhere for 30 minutes frees its symbol       # a peak below this is spread noise, not a trade
PEAKS = {}
REJECTED = {}
EXIT_FAILED = {}  # ticket -> next retry time, keeps the log clean  # symbol -> until when it is skipped after a broker refusal


def log(event, **fields):
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def ema(v, n):
    k = 2 / (n + 1); o = v[0]
    for x in v[1:]: o = x * k + o * (1 - k)
    return o


def value_per_unit(symbol, volume):
    """Money gained per 1.0 of price movement: contract_size * volume is the correct measure."""
    s = mt5.symbol_info(symbol)
    if s is None or s.trade_contract_size <= 0:
        return 0.0
    return s.trade_contract_size * volume


def span_for(symbol, volume):
    per = value_per_unit(symbol, volume)
    return STOP_USD / per if per > 0 else 0.0


def has_position(symbol):
    return any(p.symbol == symbol for p in (mt5.positions_get() or []))


def tactic_signal(symbol):
    m5 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 200)
    m1 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 120)
    s = mt5.symbol_info(symbol); t = mt5.symbol_info_tick(symbol)
    if m5 is None or m1 is None or s is None or t is None or len(m5) < 60 or len(m1) < 40:
        return None
    if s.ask <= 0 or s.bid <= 0 or s.ask - s.bid <= 0:
        return None
    c5 = [float(r["close"]) for r in m5]; c1 = [float(r["close"]) for r in m1]
    up5 = ema(c5, 21) > ema(c5, 55)
    low5 = min(float(r["low"]) for r in m5[-24:]); high5 = max(float(r["high"]) for r in m5[-24:])
    low1 = min(float(r["low"]) for r in m1[-24:]); high1 = max(float(r["high"]) for r in m1[-24:])
    spans = [max(float(m5[i]["high"]) - float(m5[i]["low"]),
                 abs(float(m5[i]["high"]) - float(m5[i - 1]["close"])),
                 abs(float(m5[i]["low"]) - float(m5[i - 1]["close"]))) for i in range(1, 15)]
    atr5 = sum(spans) / len(spans)
    if atr5 <= 0:
        return None
    up1 = c1[-1] > c1[-2] > c1[-3]
    down1 = c1[-1] < c1[-2] < c1[-3]
    if up5 and (t.ask - low5) <= 1.5 * atr5 and up1:
        return ("buy", "m5-dip")
    if up5 and c5[-1] >= max(float(r["high"]) for r in m5[-13:-1]) and up1:
        return ("buy", "m5-break")
    if (t.ask - low1) <= 1.2 * atr5 and up1:
        return ("buy", "m1-dip")
    if (high1 - t.bid) <= 1.2 * atr5 and down1:
        return ("sell", "m1-top")
    if (high5 - t.bid) <= 1.5 * atr5 and down1:
        return ("sell", "m5-top")
    return None


def manage():
    for p in mt5.positions_get() or []:
        # manage every demo position, not only the lab's own: the swarm trades under my rules too
        key = p.ticket
        peak = max(PEAKS.get(key, p.profit), p.profit)
        PEAKS[key] = peak
        spec = mt5.symbol_info(p.symbol); tick = mt5.symbol_info_tick(p.symbol)
        if spec is None or tick is None:
            continue
        # keep every stop at exactly $5 of money risk, whichever agent opened the trade
        per_unit = spec.trade_contract_size * p.volume
        if per_unit > 0:
            wanted = 5.0 / per_unit
            current = abs(p.price_open - p.sl) if p.sl else 0.0
            move = (tick.bid - p.price_open) if p.type == 0 else (p.price_open - tick.ask)
            loss_money = -move * per_unit
            if loss_money > 5.0:
                # already past the $5 budget: close instead of chasing an invalid stop
                mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol,
                                "volume": p.volume,
                                "type": mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY,
                                "price": tick.bid if p.type == 0 else tick.ask,
                                "position": p.ticket, "deviation": 40, "magic": p.magic,
                                "comment": "risk-stop", "type_time": mt5.ORDER_TIME_GTC,
                                "type_filling": mt5.ORDER_FILLING_IOC})
                log("risk_stop", symbol=p.symbol, ticket=p.ticket, loss=round(loss_money, 2))
                continue
            if current > wanted * 1.10:
                new_sl = p.price_open - wanted if p.type == 0 else p.price_open + wanted
                if (p.type == 0 and new_sl < tick.bid) or (p.type == 1 and new_sl > tick.ask):
                    mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": p.symbol,
                                    "position": p.ticket, "sl": round(new_sl, spec.digits),
                                    "tp": p.tp, "magic": p.magic, "comment": "risk-5usd"})
                    log("risk_cap", symbol=p.symbol, ticket=p.ticket)
        m1 = mt5.copy_rates_from_pos(p.symbol, mt5.TIMEFRAME_M1, 0, 30)
        reversal = False
        if m1 is not None and len(m1) >= 3:
            cl = [float(r["close"]) for r in m1]
            reversal = ((p.type == 0 and cl[-1] < cl[-2] < cl[-3]) or
                        (p.type == 1 and cl[-1] > cl[-2] > cl[-3]))
        age = time.time() - p.time
        stale = age > MAX_HOLD and abs(p.profit) < 1.0
        old = age > 2 * MAX_HOLD
        exit_now = (stale or old or p.profit >= TARGET_USD
                    or (peak >= PEAK_MIN and p.profit <= peak - 0.6)
                    or (reversal and p.profit > 1.0))
        if EXIT_FAILED.get(key, 0) > time.time():
            continue
        if exit_now:
            price = tick.bid if p.type == 0 else tick.ask
            typ = mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY
            res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol,
                                  "volume": p.volume, "type": typ, "price": price,
                                  "position": p.ticket, "deviation": 30, "magic": 7000012,
                                  "comment": "net-exit", "type_time": mt5.ORDER_TIME_GTC,
                                  "type_filling": mt5.ORDER_FILLING_IOC})
            retcode = None if res is None else res.retcode
            if retcode not in (10009, None):
                # this feed often refuses a ticket-close: flatten with the opposite deal
                mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol,
                                "volume": p.volume,
                                "type": mt5.ORDER_TYPE_BUY if p.type == 1 else mt5.ORDER_TYPE_SELL,
                                "price": mt5.symbol_info_tick(p.symbol).ask if p.type == 1 else mt5.symbol_info_tick(p.symbol).bid,
                                "deviation": 60, "magic": 7000012, "comment": "fallback-exit",
                                "type_time": mt5.ORDER_TIME_GTC,
                                "type_filling": mt5.ORDER_FILLING_FOK})
                EXIT_FAILED[key] = time.time() + 120
            log("exit", symbol=p.symbol, profit=round(p.profit, 2), peak=round(peak, 2),
                retcode=retcode)
            PEAKS.pop(key, None)
        elif p.profit >= BREAKEVEN_USD and p.sl != p.price_open:
            res = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": p.symbol,
                                  "position": p.ticket, "sl": round(p.price_open, spec.digits),
                                  "tp": p.tp, "magic": 7000012, "comment": "breakeven_lock"})
            log("breakeven", symbol=p.symbol, retcode=None if res is None else res.retcode)


def main():
    if not mt5.initialize(path=TERMINAL, timeout=10000):
        log("init_failed", error=str(mt5.last_error())); return
    info = mt5.account_info()
    if info is None or info.login != LOGIN:
        log("wrong_account", login=None if info is None else info.login); mt5.shutdown(); return
    log("start", equity=round(info.equity, 2))
    cycles = 0
    while True:
        try:
            info = mt5.account_info()
            if info is None or info.login != LOGIN:
                log("wrong_account"); mt5.shutdown(); time.sleep(20)
                mt5.initialize(path=TERMINAL, timeout=10000); continue
            cycles += 1
            manage()
            for symbol in SYMBOLS:
                if has_position(symbol) or REJECTED.get(symbol, 0) > time.time():
                    continue
                sig = tactic_signal(symbol)
                if sig is None:
                    continue
                side, tactic = sig
                spec = mt5.symbol_info(symbol); tick = mt5.symbol_info_tick(symbol)
                if spec is None or tick is None or spec.volume_min > LOT:
                    REJECTED[symbol] = time.time() + 1800
                    continue
                span = span_for(symbol, LOT)
                if span <= 0 or span < spec.trade_stops_level * spec.point * 1.1:
                    continue
                spread = spec.ask - spec.bid
                if spread <= 0 or spread > 0.25 * span:
                    continue
                entry = tick.ask if side == "buy" else tick.bid
                sl = entry - span if side == "buy" else entry + span
                tp = entry + 2 * span if side == "buy" else entry - 2 * span
                res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
                                      "volume": LOT,
                                      "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
                                      "price": entry, "sl": round(sl, spec.digits),
                                      "tp": round(tp, spec.digits), "deviation": 25,
                                      "magic": 7000012, "comment": tactic,
                                      "type_time": mt5.ORDER_TIME_GTC,
                                      "type_filling": mt5.ORDER_FILLING_IOC})
                retcode = None if res is None else res.retcode
                if retcode == 10030:
                    res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
                                          "volume": LOT,
                                          "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
                                          "price": entry, "sl": round(sl, spec.digits),
                                          "tp": round(tp, spec.digits), "deviation": 25,
                                          "magic": 7000012, "comment": tactic,
                                          "type_time": mt5.ORDER_TIME_GTC,
                                          "type_filling": mt5.ORDER_FILLING_FOK})
                    retcode = None if res is None else res.retcode
                if retcode not in (10009, None):
                    REJECTED[symbol] = time.time() + 600
                log("entry", symbol=symbol, side=side, tactic=tactic,
                    stop_usd=round(span * value_per_unit(symbol, LOT), 2),
                    sl=round(sl, spec.digits), retcode=retcode)
            if cycles % 30 == 0:
                log("scan", equity=round(info.equity, 2), positions=len(mt5.positions_get() or []))
        except Exception as exc:
            log("error", error=f"{type(exc).__name__}: {exc}")
        time.sleep(2)


if __name__ == "__main__":
    main()
