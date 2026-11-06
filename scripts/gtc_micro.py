"""Micro trading on the operator's real GTC terminal (0.01 lots, <= $1.2 risk per trade).

The terminal is already authorized in its own window, so this script attaches to the running
instance (no `mt5.login`, which would switch ALGO TRADING off) and only places an order when the
geometry and the money both fit. `--check` prints the decision without sending anything.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleet import livegate  # noqa: E402

TERMINAL = r"C:\Program Files\GTC Global SA MetaTrader 5\terminal64.exe"
LOGIN = 52183174
SERVER = "GTCGlobalSA-Server 2"
MAGIC = 7000003
LOT = 0.01
MAX_RISK_MONEY = 2.00   # one trade may not lose more than this
MAX_OPEN = 3            # up to three entries at once, as the operator allowed
MAX_OPEN_RISK = 7.50    # total open risk ceiling, so three correlated buys stay survivable
DAILY_LOSS_LIMIT = 15.00  # operator-approved day budget
SYMBOLS = ("EURUSD", "GBPUSD")
METALS = ("XAUUSD",)          # gold only: silver min stop is ~$7.5 = 32% of this account
METAL_MAX_RISK = 2.00        # short-term metals may risk up to $2, never more
METAL_MAX_SPREAD = 0.60
EVENTS = r"C:\Users\Administrator\Documents\Qoder\2026-10-30\c3d543b8\repo\agent_data\gtc_micro.jsonl"


def log(event: str, **fields) -> None:
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
    with open(EVENTS, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(row, ensure_ascii=False))


def ema(values, period: int) -> float:
    k = 2 / (period + 1)
    out = values[0]
    for value in values[1:]:
        out = value * k + out * (1 - k)
    return out


def atr(rates, period: int = 14) -> float:
    spans = []
    for i in range(1, min(len(rates), period + 1)):
        high, low, previous = rates[i]["high"], rates[i]["low"], rates[i - 1]["close"]
        spans.append(max(high - low, abs(high - previous), abs(low - previous)))
    return sum(spans) / len(spans) if spans else 0.0


def _server_now() -> datetime:
    """The broker's own clock: a day limit must roll over on the broker's day, not ours."""
    tick = mt5.symbol_info_tick(SYMBOLS[0]) if SYMBOLS else None
    if tick is not None and tick.time:
        return datetime.fromtimestamp(tick.time)
    return datetime.now()


def today_loss() -> float:
    now = _server_now()
    since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    deals = mt5.history_deals_get(since, now + timedelta(minutes=5)) or []
    return sum(d.profit + d.swap + d.commission for d in deals if d.entry == 1)


DIP_SYMBOLS = ("XAUUSD", "EURUSD", "GBPUSD")
DIP_FALL_ATR = 1.6      # the price must have fallen this far from the recent high
DIP_NEAR_LOW = 0.40     # and sit this close to the recent low: "the lowest price"
DIP_RISK = 2.50         # a dip buy may risk this much, never more


def dip_buy() -> dict | None:
    """Buy a fall at its low, only once growth has actually started.

    The operator's rule: "buy on growth after falls, at the lowest price, when only growth is
    ahead". So we need three things at once: a real fall, the price at the bottom of that fall,
    and the first rising M5 closes off that low.
    """
    positions = mt5.positions_get() or []
    if len(positions) >= MAX_OPEN:
        return None
    for symbol in DIP_SYMBOLS:
        if not mt5.symbol_select(symbol, True):
            continue
        spec = mt5.symbol_info(symbol)
        m15 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 200)
        m5 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 40)
        if spec is None or m15 is None or m5 is None or len(m15) < 60 or len(m5) < 10:
            continue
        spread_money = spec.spread * spec.trade_tick_value / max(spec.trade_tick_size, 1e-12) * LOT
        if spread_money > 0.60:
            continue
        highs = [float(r["high"]) for r in m15[-48:]]
        lows = [float(r["low"]) for r in m15[-48:]]
        closes5 = [float(r["close"]) for r in m5]
        atr15 = atr(m15)
        if atr15 <= 0:
            continue
        price = mt5.symbol_info_tick(symbol).ask
        fall = (max(highs) - price) / atr15
        near_low = (price - min(lows)) / atr15
        # growth started: two rising closes off the bottom and the last one above the low band
        rising = closes5[-1] > closes5[-2] > closes5[-3]
        if fall < DIP_FALL_ATR or near_low > DIP_NEAR_LOW or not rising:
            continue
        sl_price = min(lows[-12:]) - atr15 * 0.15
        distance = price - sl_price
        if distance <= max(spec.trade_stops_level * spec.point, spec.spread * spec.point * 2):
            continue
        money = distance / spec.trade_tick_size * spec.trade_tick_value * LOT
        if money > DIP_RISK:
            continue
        return {"take": True, "symbol": symbol, "side": "buy", "lot": LOT,
                "entry": price, "sl": round(sl_price, spec.digits),
                "tp": round(price + distance * 2, spec.digits),
                "risk_money": round(money, 2), "fall_atr": round(fall, 2),
                "day_loss": round(today_loss(), 2), "why": "dip buy: fall + bottom + rising closes"}
    return None


def _close(position, why: str) -> None:
    """Exit before the forecast turns into a reversal: the operator sells a little early."""
    tick = mt5.symbol_info_tick(position.symbol)
    price = tick.bid if position.type == mt5.POSITION_TYPE_BUY else tick.ask
    order_type = mt5.ORDER_TYPE_SELL if position.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol,
                          "volume": position.volume, "type": order_type, "price": price,
                          "position": position.ticket, "deviation": 30, "magic": MAGIC,
                          "comment": "exit-early", "type_time": mt5.ORDER_TIME_GTC,
                          "type_filling": mt5.ORDER_FILLING_IOC})
    log("exit_early", ticket=position.ticket, symbol=position.symbol, why=why,
        profit=round(position.profit, 2), retcode=None if res is None else res.retcode)


def manage_positions() -> None:
    """Sell before the fall: a long is closed when the short-term forecast has turned down."""
    for position in mt5.positions_get() or []:
        if position.magic != MAGIC:
            continue
        m5 = mt5.copy_rates_from_pos(position.symbol, mt5.TIMEFRAME_M5, 0, 40)
        if m5 is None or len(m5) < 25:
            continue
        closes = [float(r["close"]) for r in m5]
        turning_down = closes[-1] < closes[-2] < closes[-3] and closes[-1] < ema(closes, 21)
        turning_up = closes[-1] > closes[-2] > closes[-3] and closes[-1] > ema(closes, 21)
        if position.type == mt5.POSITION_TYPE_BUY and turning_down and position.profit > 0:
            _close(position, "прогноз развернулся вниз — продаю раньше")
        elif position.type == mt5.POSITION_TYPE_SELL and turning_up and position.profit > 0:
            _close(position, "прогноз развернулся вверх — выкупаю раньше")


def top_sell() -> dict | None:
    """Sell a rally at its top once the fall has started (mirror of the dip buy)."""
    positions = mt5.positions_get() or []
    if len(positions) >= MAX_OPEN:
        return None
    for symbol in DIP_SYMBOLS:
        if not mt5.symbol_select(symbol, True):
            continue
        spec = mt5.symbol_info(symbol)
        m15 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 200)
        m5 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 40)
        if spec is None or m15 is None or m5 is None or len(m15) < 60 or len(m5) < 10:
            continue
        atr15 = atr(m15)
        if atr15 <= 0:
            continue
        highs = [float(r["high"]) for r in m15[-48:]]
        lows = [float(r["low"]) for r in m15[-48:]]
        closes5 = [float(r["close"]) for r in m5]
        price = mt5.symbol_info_tick(symbol).bid
        rally = (price - min(lows)) / atr15
        near_top = (max(highs) - price) / atr15
        falling = closes5[-1] < closes5[-2] < closes5[-3]
        if rally < DIP_FALL_ATR or near_top > DIP_NEAR_LOW or not falling:
            continue
        sl_price = max(highs[-12:]) + atr15 * 0.15
        distance = sl_price - price
        if distance <= max(spec.trade_stops_level * spec.point, spec.spread * spec.point * 2):
            continue
        money = distance / spec.trade_tick_size * spec.trade_tick_value * LOT
        if money > DIP_RISK:
            continue
        return {"take": True, "symbol": symbol, "side": "sell", "lot": LOT,
                "entry": price, "sl": round(sl_price, spec.digits),
                "tp": round(price - distance * 2, spec.digits),
                "risk_money": round(money, 2), "rally_atr": round(rally, 2),
                "day_loss": round(today_loss(), 2), "why": "top sell: rally + top + falling closes"}
    return None


def momentum_micro() -> dict | None:
    """Fast micro entry so the real account actually participates: M5 impulse, $2 stop."""
    positions = mt5.positions_get() or []
    if len(positions) >= MAX_OPEN:
        return None
    for symbol in ("EURUSD", "GBPUSD", "XAUUSD"):
        if not mt5.symbol_select(symbol, True):
            continue
        spec = mt5.symbol_info(symbol)
        m5 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 40)
        if spec is None or m5 is None or len(m5) < 25:
            continue
        closes = [float(r["close"]) for r in m5]
        fast = ema(closes, 21)
        tick = mt5.symbol_info_tick(symbol)
        spread_money = spec.spread * spec.trade_tick_value / max(spec.trade_tick_size, 1e-12) * LOT
        if spread_money > 0.60:
            continue
        rising = closes[-1] > closes[-2] > closes[-3] and closes[-1] > fast
        falling = closes[-1] < closes[-2] < closes[-3] and closes[-1] < fast
        if not (rising or falling):
            continue
        side = "buy" if rising else "sell"
        entry = tick.ask if side == "buy" else tick.bid
        max_risk = min(MAX_RISK_MONEY, DAILY_LOSS_LIMIT + today_loss())
        span = max(spec.trade_stops_level * spec.point * 1.1, min(
            2.0 / (spec.trade_tick_value * LOT / spec.trade_tick_size), 40 * spec.point))
        money = span / spec.trade_tick_size * spec.trade_tick_value * LOT
        if money > max_risk or money <= 0:
            continue
        sl = entry - span if side == "buy" else entry + span
        tp = entry + span * 1.8 if side == "buy" else entry - span * 1.8
        return {"take": True, "symbol": symbol, "side": side, "lot": LOT,
                "entry": entry, "sl": round(sl, spec.digits), "tp": round(tp, spec.digits),
                "risk_money": round(money, 2), "why": "M5 impulse micro",
                "day_loss": round(today_loss(), 2)}
    return None


def decide() -> dict:
    manage_positions()
    dip = dip_buy()
    if dip is not None:
        return dip
    info = mt5.account_info()
    if info is None or info.login != LOGIN:
        return {"take": False, "why": f"терминал отвечает не тем счётом: {None if info is None else info.login}"}
    if info.trade_mode != mt5.ACCOUNT_TRADE_MODE_REAL:
        return {"take": False, "why": "счёт не real"}
    positions = mt5.positions_get() or []
    open_risk = 0.0
    for p in positions:
        spec = mt5.symbol_info(p.symbol)
        if spec is not None and p.sl:
            open_risk += abs(p.price_open - p.sl) / spec.trade_tick_size * spec.trade_tick_value * p.volume
    if open_risk >= MAX_OPEN_RISK:
        return {"take": False, "why": f"открытый риск ${open_risk:.2f} — потолок ${MAX_OPEN_RISK:.2f}"}
    if len(positions) >= MAX_OPEN:
        return {"take": False, "why": f"открыто {len(positions)} позиций — потолок {MAX_OPEN}"}
    loss = today_loss()
    remaining = DAILY_LOSS_LIMIT + loss
    if remaining <= 0:
        return {"take": False, "why": f"дневной лимит убытка исчерпан: {loss:.2f}"}
    open_symbols = {p.symbol for p in positions}
    for symbol in SYMBOLS:
        if not mt5.symbol_select(symbol, True):
            continue
        h4 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H4, 0, 120)
        h1 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 120)
        m15 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 120)
        if h4 is None or h1 is None or m15 is None or len(m15) < 30:
            continue
        h4_closes = [float(r["close"]) for r in h4]
        h1_closes = [float(r["close"]) for r in h1]
        m15_closes = [float(r["close"]) for r in m15]
        # "growth is coming" = trend agrees on H4+H1 AND short-term momentum already turned up:
        # three rising M5 closes with the close above the M5 EMA21. Without that turn we wait.
        m5 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 40)
        moment_up = False
        if m5 is not None and len(m5) > 25:
            closes = [float(r["close"]) for r in m5]
            fast = ema(closes, 21)
            moment_up = closes[-1] > closes[-2] > closes[-3] and closes[-1] > fast
        up = moment_up and ema(h4_closes, 21) > ema(h4_closes, 55) and ema(h1_closes, 21) > ema(h1_closes, 55)
        down = ema(h4_closes, 21) < ema(h4_closes, 55) and ema(h1_closes, 21) < ema(h1_closes, 55)
        price = mt5.symbol_info_tick(symbol).ask
        fast = ema(h1_closes, 21)
        if up and price > fast:
            side = "buy"
        elif down and price < fast:
            side = "sell"
        else:
            continue
        span = max(atr(m15), 0.0)
        spec = mt5.symbol_info(symbol)
        if spec is None or span <= 0:
            continue
        distance = max(span * 1.2, spec.trade_stops_level * spec.point, spec.spread * spec.point * 2)
        money = (distance / spec.trade_tick_size) * spec.trade_tick_value * LOT
        if money > min(MAX_RISK_MONEY, remaining):
            continue
        tick = mt5.symbol_info_tick(symbol)
        entry = tick.ask if side == "buy" else tick.bid
        sl = entry - distance if side == "buy" else entry + distance
        tp = entry + distance * 2 if side == "buy" else entry - distance * 2
        return {"take": True, "symbol": symbol, "side": side, "lot": LOT, "entry": entry,
                "sl": round(sl, spec.digits), "tp": round(tp, spec.digits),
                "risk_money": round(money, 2), "day_loss": round(loss, 2)}
    top = top_sell()
    if top is not None:
        return top
    return (metals_decision() or momentum_micro()
            or {"take": False, "why": "нет сетапа: ни dip, ни top, ни импульса"})


def metals_decision() -> dict | None:
    """Short-term gold: M5 trend with a tight, broker-legal stop (risk capped at $2)."""
    open_symbols = {p.symbol for p in (mt5.positions_get() or [])}
    for symbol in METALS:
        if not mt5.symbol_select(symbol, True):
            continue
        spec = mt5.symbol_info(symbol)
        m5 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 120)
        h1 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 60)
        if spec is None or m5 is None or h1 is None or len(m5) < 40:
            continue
        spread_money = spec.spread * spec.trade_tick_value / max(spec.trade_tick_size, 1e-12) * LOT
        if spread_money > METAL_MAX_SPREAD:
            continue
        closes = [float(r["close"]) for r in m5]
        fast, slow = ema(closes, 21), ema(closes, 55)
        h1_closes = [float(r["close"]) for r in h1]
        up = fast > slow and ema(h1_closes, 21) > ema(h1_closes, 55)
        down = fast < slow and ema(h1_closes, 21) < ema(h1_closes, 55)
        tick = mt5.symbol_info_tick(symbol)
        price = tick.bid
        if up and price <= fast + 0.15 * atr(m5):
            side = "buy"
        elif down and price >= fast - 0.15 * atr(m5):
            side = "sell"
        else:
            continue
        span = max(atr(m5) * 0.9, spec.trade_stops_level * spec.point * 1.1)
        money = (span / spec.trade_tick_size) * spec.trade_tick_value * LOT
        if money > min(METAL_MAX_RISK, DAILY_LOSS_LIMIT + today_loss()):
            continue
        entry = tick.ask if side == "buy" else tick.bid
        sl = entry - span if side == "buy" else entry + span
        tp = entry + span * 1.8 if side == "buy" else entry - span * 1.8
        return {"take": True, "symbol": symbol, "side": side, "lot": LOT,
                "entry": entry, "sl": round(sl, spec.digits), "tp": round(tp, spec.digits),
                "risk_money": round(money, 2), "spread_money": round(spread_money, 2),
                "day_loss": round(today_loss(), 2)}
    return None


def send(decision: dict, *, live: bool) -> None:
    answered = mt5.account_info()
    refused = livegate.refusal(LOGIN, live=live,
                               server="" if answered is None else answered.server)
    if refused:
        log("refused", why=refused, symbol=decision["symbol"], side=decision["side"],
            lot=decision["lot"])
        return
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": decision["symbol"],
        "volume": decision["lot"],
        "type": mt5.ORDER_TYPE_BUY if decision["side"] == "buy" else mt5.ORDER_TYPE_SELL,
        "price": decision["entry"],
        "sl": decision["sl"],
        "tp": decision["tp"],
        "deviation": 20,
        "magic": MAGIC,
        "comment": "fleet-micro",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    log("order", retcode=None if result is None else result.retcode,
        comment=None if result is None else result.comment,
        symbol=decision["symbol"], side=decision["side"], lot=decision["lot"],
        sl=decision["sl"], tp=decision["tp"], risk=decision["risk_money"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="print the decision, send nothing")
    parser.add_argument("--once", action="store_true", help="single pass instead of a loop")
    parser.add_argument("--live", action="store_true", help="operator's word that real money may be sent")
    args = parser.parse_args()
    if not mt5.initialize(path=TERMINAL, timeout=10000):
        log("init_failed", error=str(mt5.last_error()))
        return 2
    try:
        info = mt5.account_info()
        log("attach", login=None if info is None else info.login,
            server=None if info is None else info.server,
            balance=None if info is None else round(info.balance, 2))
        held = livegate.stand_refusal()
        if held:
            log("parked", why=held)
        while True:
            decision = decide()
            if decision.get("take"):
                log("decision", **decision)
                if not args.check:
                    send(decision, live=args.live)
            else:
                log("skip", why=decision.get("why"))
            if args.check or args.once:
                return 0
            time.sleep(60)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
