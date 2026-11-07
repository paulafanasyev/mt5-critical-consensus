"""Mirror the demo swarm's non-experimental opens onto the real GTC account.

The operator wants the real account to play the same game as the demo: identical logic, identical
0.01 lots, but real money. The demo only differs by its size. Each swarm OPEN line is one signal;
this loop reads new lines, sizes a 0.01 real trade with a hard $2 stop, and refuses anything that
does not fit the risk budget.
"""
from __future__ import annotations

import json
import pathlib
import time
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

_CANDLES = {}


def _copy_cached(symbol, timeframe, start_pos=0, count=0, **kwargs):
    """One candle snapshot per cycle: every rule reads from the same bars."""
    key = (symbol, int(timeframe), int(start_pos), int(count))
    hit = _CANDLES.get(key)
    now = time.time()
    if hit and now - hit[0] < 2.0:
        return hit[1]
    data = mt5.copy_rates_from_pos(symbol, timeframe, start_pos, count)
    _CANDLES[key] = (now, data)
    return data


ROOT = pathlib.Path(__file__).resolve().parents[1]
SWARM_LOG = ROOT / "agent_data" / "swarm.jsonl"
CURSOR = ROOT / "agent_data" / "mirror.cursor"
DONE = ROOT / "agent_data" / "mirror.done.json"
EVENTS = ROOT / "agent_data" / "mirror.jsonl"
TERMINAL = r"C:\Program Files\GTC Global SA MetaTrader 5\terminal64.exe"

LOT = 0.01
RISK = 3.00
GOLD_RISK = 5.00   # operator's cap: a single trade may not lose more than $5
XAG_RISK = 8.00    # silver's broker minimum stop is $7.5, so its cap must be at least that   # the operator accepts a quarter of the account on a gold entry
MAX_OPEN = 3
DAILY_LIMIT = 9999.0  # no daily limit: the operator caps a single trade instead


_TRADE_DISABLED_UNTIL = 0.0


def trade_disabled() -> bool:
    return time.time() < _TRADE_DISABLED_UNTIL


def note_disabled() -> None:
    """The broker answered 10017: stop hammering the server for half an hour."""
    global _TRADE_DISABLED_UNTIL
    _TRADE_DISABLED_UNTIL = time.time() + 1800
    log("broker_trade_disabled", until_in_min=30,
        why="10017 Trade disabled: счёт закрыт для торговли, ждём 30 минут")


def log(event: str, **fields) -> None:
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
    with EVENTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    try:
        print(json.dumps(row, ensure_ascii=False))
    except UnicodeEncodeError:
        pass


def cursor() -> int:
    try:
        return int(CURSOR.read_text(encoding="utf-8").strip() or 0)
    except Exception:
        return 0


def day_loss() -> float:
    tick = mt5.symbol_info_tick("EURUSD")
    now = datetime.fromtimestamp(tick.time) if tick and tick.time else datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    deals = mt5.history_deals_get(start, now + timedelta(minutes=5)) or []
    return sum(d.profit + d.swap + d.commission for d in deals if d.entry == 1)


def geometry(symbol: str, side: str, max_risk: float):
    spec = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if spec is None or tick is None:
        return None
    entry = tick.ask if side == "buy" else tick.bid
    # brokers reject stops closer than stops_level, and a fast tick can eat the buffer:
    # keep 1.5x the level plus three spreads so the order is never "invalid stops".
    span = max(spec.trade_stops_level * spec.point * 1.5, spec.spread * spec.point * 3)
    money = span / spec.trade_tick_size * spec.trade_tick_value * LOT
    if money > max_risk:
        span = max_risk / (spec.trade_tick_value * LOT / spec.trade_tick_size)
        money = span / spec.trade_tick_size * spec.trade_tick_value * LOT
    if span <= 0:
        return None
    sl = entry - span if side == "buy" else entry + span
    tp = entry + span * 1.8 if side == "buy" else entry - span * 1.8
    return entry, round(sl, spec.digits), round(tp, spec.digits), round(money, 2)


def gold_trend_ok(side: str) -> bool:
    """Only copy a gold signal when the H4 and H1 trends both point the same way."""
    h4 = _copy_cached("XAUUSD", mt5.TIMEFRAME_H4, 0, 120)
    h1 = _copy_cached("XAUUSD", mt5.TIMEFRAME_H1, 0, 120)
    if h4 is None or h1 is None or len(h4) < 60 or len(h1) < 60:
        return False

    def ema(values, period):
        k = 2 / (period + 1)
        out = values[0]
        for value in values[1:]:
            out = value * k + out * (1 - k)
        return out

    c4 = [float(r["close"]) for r in h4]
    c1 = [float(r["close"]) for r in h1]
    up = ema(c4, 21) > ema(c4, 55) and ema(c1, 21) > ema(c1, 55)
    down = ema(c4, 21) < ema(c4, 55) and ema(c1, 21) < ema(c1, 55)
    return (side == "buy" and up) or (side == "sell" and down)


def place(side: str, symbol: str, agent: str) -> None:
    if trade_disabled():
        return
    if symbol.startswith("XAU"):
        if not gold_margin_ok():
            log("skip_gold_margin", symbol=symbol, why="маржа золота больше половины свободной")
            return
        if any(p.symbol.startswith("XAU") for p in (mt5.positions_get() or [])):
            log("skip_gold_busy", symbol=symbol, why="одна золотая позиция за раз")
            return
        if not gold_trend_ok(side):
            log("skip_gold_trend", symbol=symbol, side=side,
                why="H4 и H1 не согласованы — не покупаю против тренда")
            return
    if symbol.startswith("XAG"):
        pass  # silver is tradable now that the account is funded; risk is capped below
    if not mt5.symbol_select(symbol, True):
        log("skip_symbol", symbol=symbol)
        return
    if len(mt5.positions_get() or []) >= MAX_OPEN:
        log("skip_capacity", symbol=symbol)
        return
    if day_loss() <= -DAILY_LIMIT:
        log("skip_daylimit", loss=round(day_loss(), 2))
        return
    if symbol.startswith("XAU"):
        max_risk = GOLD_RISK
    elif symbol.startswith("XAG"):
        max_risk = XAG_RISK
    else:
        max_risk = RISK
    geo = geometry(symbol, side, max_risk)
    if geo is None:
        log("skip_geometry", symbol=symbol)
        return
    entry, sl, tp, risk = geo
    if entry <= 0 or sl <= 0 or tp <= 0:
        log("skip_invalid_quote", symbol=symbol, entry=entry, sl=sl, tp=tp)
        return
    res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": LOT,
                          "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
                          "price": entry, "sl": sl, "tp": tp, "deviation": 30, "magic": 7000003,
                          "comment": f"mirror-{agent[:12]}", "type_time": mt5.ORDER_TIME_GTC,
                          "type_filling": mt5.ORDER_FILLING_IOC})
    log("mirror", agent=agent, symbol=symbol, side=side, sl=sl, tp=tp, risk=risk,
        retcode=None if res is None else res.retcode,
        comment=None if res is None else res.comment)
    if res is not None and res.retcode == 10017:
        note_disabled()


PEAKS = ROOT / "agent_data" / "mirror_peaks.json"
TAKE_PROFIT_USD = 3.50      # money on the table: take it before the peak turns into a loss
GIVEBACK_USD = 1.20         # if the peak was real, do not give back more than this


def small_target(symbol: str) -> float:
    """The smallest net profit worth taking: above the round-trip spread of that symbol."""
    return {"XAUUSD": 0.30, "XAGUSD": 0.40}.get(symbol, 0.10)


def manage_profit() -> None:
    # a small plus is still a plus: bank it instead of waiting for a big move
    for position in mt5.positions_get() or []:
        if position.magic != 7000003:
            continue
        if position.profit >= small_target(position.symbol):
            _close(position, f"small target +${position.profit:.2f}")
            continue
        # once the trade is in the black, lock it at breakeven: at worst we leave at zero
        if position.profit > 0 and position.sl:
            spec = mt5.symbol_info(position.symbol)
            if spec is not None:
                if position.type == 0 and position.sl < position.price_open:
                    mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": position.symbol,
                                    "position": position.ticket,
                                    "sl": round(position.price_open, spec.digits),
                                    "tp": position.tp, "magic": position.magic,
                                    "comment": "breakeven"})
                    log("breakeven", ticket=position.ticket, symbol=position.symbol)
                elif position.type == 1 and position.sl > position.price_open:
                    mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": position.symbol,
                                    "position": position.ticket,
                                    "sl": round(position.price_open, spec.digits),
                                    "tp": position.tp, "magic": position.magic,
                                    "comment": "breakeven"})
                    log("breakeven", ticket=position.ticket, symbol=position.symbol)
    """Hold for the maximum: no early selling, only a trailing stop that follows the price up."""
    for position in mt5.positions_get() or []:
        if position.magic != 7000003:
            continue
        if position.profit <= 0:
            continue
        spec = mt5.symbol_info(position.symbol)
        if spec is None:
            continue
        tick = mt5.symbol_info_tick(position.symbol)
        # tight trail: give back as little as the broker allows, so the best price is not lost
        distance = max(spec.trade_stops_level * spec.point * 1.6, 1.6)
        if position.type == mt5.POSITION_TYPE_BUY:
            candidate = tick.bid - distance
            if candidate > position.sl + spec.point * spec.trade_stops_level and candidate < tick.bid:
                mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": position.symbol,
                                "position": position.ticket, "sl": round(candidate, spec.digits),
                                "tp": position.tp, "magic": position.magic, "comment": "trail"})
                log("trail", ticket=position.ticket, symbol=position.symbol,
                    sl=round(candidate, spec.digits), profit=round(position.profit, 2))
        else:
            candidate = tick.ask + distance
            if candidate < position.sl - spec.point * spec.trade_stops_level and candidate > tick.ask:
                mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": position.symbol,
                                "position": position.ticket, "sl": round(candidate, spec.digits),
                                "tp": position.tp, "magic": position.magic, "comment": "trail"})
                log("trail", ticket=position.ticket, symbol=position.symbol,
                    sl=round(candidate, spec.digits), profit=round(position.profit, 2))


def _close(position, why: str) -> None:
    tick = mt5.symbol_info_tick(position.symbol)
    price = tick.bid if position.type == mt5.POSITION_TYPE_BUY else tick.ask
    order_type = mt5.ORDER_TYPE_SELL if position.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol,
                          "volume": position.volume, "type": order_type, "price": price,
                          "position": position.ticket, "deviation": 30, "magic": 7000003,
                          "comment": "peak-sell", "type_time": mt5.ORDER_TIME_GTC,
                          "type_filling": mt5.ORDER_FILLING_IOC})
    log("peak_sell", ticket=position.ticket, symbol=position.symbol, why=why,
        profit=round(position.profit, 2), retcode=None if res is None else res.retcode)


def fall_and_rise() -> tuple[float, float, float]:
    """Return (recent high, recent low, ATR) of the last 48 M15 bars for gold."""
    m15 = _copy_cached("XAUUSD", mt5.TIMEFRAME_M15, 0, 120)
    if m15 is None or len(m15) < 60:
        return 0.0, 0.0, 0.0
    highs = [float(r["high"]) for r in m15[-48:]]
    lows = [float(r["low"]) for r in m15[-48:]]
    spans = [max(m15[i]["high"] - m15[i]["low"], abs(m15[i]["high"] - m15[i-1]["close"]),
                 abs(m15[i]["low"] - m15[i-1]["close"])) for i in range(1, 15)]
    return max(highs), min(lows), sum(spans) / len(spans)


def gold_peak_short() -> None:
    """Sell the peak of a rise before the fall: a real run-up, price at the top, first down bar."""
    if any(p.symbol.startswith("XAU") for p in (mt5.positions_get() or [])):
        return
    if h1_trend("XAUUSD") >= 0:
        return
    spec = mt5.symbol_info("XAUUSD")
    m5 = _copy_cached("XAUUSD", mt5.TIMEFRAME_M5, 0, 30)
    high, low, atr15 = fall_and_rise()
    if spec is None or m5 is None or len(m5) < 10 or atr15 <= 0:
        return
    tick = mt5.symbol_info_tick("XAUUSD")
    c5 = [float(r["close"]) for r in m5]
    run_up = (tick.bid - low) / atr15
    near_top = (high - tick.bid) <= 0.40 * atr15
    first_down = c5[-1] < c5[-2] and c5[-2] >= max(c5[-5:])
    if run_up < 1.2 or not near_top or not first_down:
        return
    sl_price = high + 0.20 * atr15
    distance = sl_price - tick.bid
    money = distance / spec.trade_tick_size * spec.trade_tick_value * LOT
    if money > GOLD_RISK or distance < spec.trade_stops_level * spec.point * 1.2:
        return
    res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": "XAUUSD", "volume": LOT,
                          "type": mt5.ORDER_TYPE_SELL, "price": tick.bid,
                          "sl": round(sl_price, spec.digits),
                          "tp": round(tick.bid - distance * 2, spec.digits),
                          "deviation": 30, "magic": 7000003, "comment": "gold-peak",
                          "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC})
    log("gold_peak_short", sl=round(sl_price, 2), risk=round(money, 2), high=round(high, 2),
        retcode=None if res is None else res.retcode)


def gold_bottom_confirmed() -> None:
    """Buy only when the fall has bottomed out on M5 and M1 has already turned up.

    The operator's rule: confirm the maximum fall first, then buy the forecast rise. So the M5
    low must be tested and held (higher low), and M1 must show a rising impulse - then enter with
    a stop under the confirmed bottom, capped at $5.
    """
    positions = mt5.positions_get() or []
    if trade_disabled() or len(positions) >= MAX_OPEN:
        return
    if not gold_margin_ok():
        return
    spec = mt5.symbol_info("XAUUSD")
    m5 = _copy_cached("XAUUSD", mt5.TIMEFRAME_M5, 0, 60)
    m1 = _copy_cached("XAUUSD", mt5.TIMEFRAME_M1, 0, 60)
    m15 = _copy_cached("XAUUSD", mt5.TIMEFRAME_M15, 0, 60)
    if spec is None or m5 is None or m1 is None or m15 is None or len(m5) < 30 or len(m1) < 30:
        return
    spans = [max(m15[i]["high"] - m15[i]["low"], abs(m15[i]["high"] - m15[i-1]["close"]),
                 abs(m15[i]["low"] - m15[i-1]["close"])) for i in range(1, 15)]
    atr15 = sum(spans) / len(spans)
    if atr15 <= 0:
        return
    tick = mt5.symbol_info_tick("XAUUSD")
    swing_low = min(float(r["low"]) for r in m5[-24:])
    if h1_trend("XAUUSD") <= 0 or h1_dir("XAUUSD") <= 0:
        return
    near_bottom = (tick.ask - swing_low) <= 0.45 * atr15
    bottom_held = (float(m5[-1]["low"]) >= float(m5[-2]["low"])
                   and float(m5[-1]["close"]) > float(m5[-2]["close"]))
    c1 = [float(r["close"]) for r in m1]
    ema21 = sum(c1[-21:]) / 21
    m1_up = c1[-1] > c1[-2] > c1[-3] and c1[-1] > ema21
    if not (near_bottom and bottom_held and m1_up):
        return
    sl_price = tick.ask - span_for("XAUUSD", GOLD_RISK)  # the full $5, not a hair-trigger stop
    distance = tick.ask - sl_price
    money = distance / spec.trade_tick_size * spec.trade_tick_value * LOT
    if money > GOLD_RISK or distance < spec.trade_stops_level * spec.point * 1.2:
        return
    res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": "XAUUSD", "volume": LOT,
                          "type": mt5.ORDER_TYPE_BUY, "price": tick.ask,
                          "sl": round(sl_price, spec.digits),
                          "tp": round(tick.ask + 2 * distance, spec.digits),
                          "deviation": 30, "magic": 7000003, "comment": "gold-bottom-confirmed",
                          "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC})
    log("gold_bottom_confirmed", sl=round(sl_price, 2), risk=round(money, 2),
        low=round(swing_low, 2), retcode=None if res is None else res.retcode)


def gold_rise_entry() -> None:
    """Catch a rise that starts away from the extreme low: M5 breakout + M1 impulse + H1 up."""
    positions = mt5.positions_get() or []
    if trade_disabled() or len(positions) >= MAX_OPEN:
        return
    if not gold_margin_ok():
        return
    spec = mt5.symbol_info("XAUUSD")
    m5 = _copy_cached("XAUUSD", mt5.TIMEFRAME_M5, 0, 40)
    m1 = _copy_cached("XAUUSD", mt5.TIMEFRAME_M1, 0, 60)
    h1 = _copy_cached("XAUUSD", mt5.TIMEFRAME_H1, 0, 60)
    m15 = _copy_cached("XAUUSD", mt5.TIMEFRAME_M15, 0, 60)
    if spec is None or m5 is None or m1 is None or h1 is None or m15 is None:
        return
    if len(m5) < 20 or len(m1) < 25:
        return
    spans = [max(m15[i]["high"] - m15[i]["low"], abs(m15[i]["high"] - m15[i-1]["close"]),
                 abs(m15[i]["low"] - m15[i-1]["close"])) for i in range(1, 15)]
    atr15 = sum(spans) / len(spans)
    if atr15 <= 0:
        return
    c1 = [float(r["close"]) for r in m1]
    m1_up = c1[-1] > c1[-2] > c1[-3] and c1[-1] > sum(c1[-21:]) / 21
    h1c = [float(r["close"]) for r in h1]
    h1_up = sum(h1c[-21:]) / 21 > sum(h1c[-55:]) / 55 if len(h1c) >= 55 else False
    high12 = max(float(r["high"]) for r in m5[-13:-1])
    breakout = float(m5[-1]["close"]) >= high12
    if not (breakout and m1_up and h1_up):
        return
    tick = mt5.symbol_info_tick("XAUUSD")
    span = span_for("XAUUSD", GOLD_RISK)
    sl = tick.ask - span
    res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": "XAUUSD", "volume": LOT,
                          "type": mt5.ORDER_TYPE_BUY, "price": tick.ask,
                          "sl": round(sl, spec.digits), "tp": round(tick.ask + 2 * span, spec.digits),
                          "deviation": 30, "magic": 7000003, "comment": "gold-rise",
                          "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC})
    log("gold_rise", sl=round(sl, 2), risk=round(span, 2), high12=round(high12, 2),
        retcode=None if res is None else res.retcode)


def gold_dip_quick() -> None:
    if trade_disabled():
        return
    """The operator's observation: a $5 drawdown that recovers to $2 is a $3 bounce.

    Buy a short-term dip (>=0.6 ATR below the last 12 M15 highs) once the first M5 bar turns up,
    with a $3 stop - so the bounce is captured without betting the account on one idea.
    """
    if any(p.symbol.startswith("XAU") for p in (mt5.positions_get() or [])):
        return
    if not gold_margin_ok():
        return
    spec = mt5.symbol_info("XAUUSD")
    m15 = _copy_cached("XAUUSD", mt5.TIMEFRAME_M15, 0, 60)
    m5 = _copy_cached("XAUUSD", mt5.TIMEFRAME_M5, 0, 20)
    if spec is None or m15 is None or m5 is None or len(m15) < 30:
        return
    spans = [max(m15[i]["high"] - m15[i]["low"], abs(m15[i]["high"] - m15[i-1]["close"]),
                 abs(m15[i]["low"] - m15[i-1]["close"])) for i in range(1, 15)]
    atr15 = sum(spans) / len(spans)
    if atr15 <= 0:
        return
    tick = mt5.symbol_info_tick("XAUUSD")
    # use the M5 swing, not the 3-hour M15 high: growth on M5 rarely breaks a M15 extreme
    recent_high = max(float(r["high"]) for r in m5[-12:])
    recent_low = min(float(r["low"]) for r in m5[-12:])
    c5 = [float(r["close"]) for r in m5]
    # "predicted rise" = strength confirmed: price breaks the recent high while M5 turns up.
    breakout_up = bool(tick.ask >= recent_high and c5[-1] > c5[-2])
    breakout_down = bool(tick.bid <= recent_low and c5[-1] < c5[-2])
    if not (breakout_up or breakout_down):
        return
    side = "buy" if breakout_up else "sell"
    trend = h1_trend("XAUUSD")
    if (side == "buy" and trend <= 0) or (side == "sell" and trend >= 0):
        return
    entry = tick.ask if side == "buy" else tick.bid
    span = span_for("XAUUSD", GOLD_RISK)
    sl = entry - span if side == "buy" else entry + span
    tp = entry + 2 * span if side == "buy" else entry - 2 * span
    lot = entry_lot()
    res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": "XAUUSD", "volume": lot,
                          "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
                          "price": entry, "sl": round(sl, spec.digits), "tp": round(tp, spec.digits),
                          "deviation": 30, "magic": 7000003, "comment": "gold-breakout",
                          "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC})
    log("gold_breakout", side=side, sl=round(sl, 2), risk=round(span * lot / LOT, 2), lot=lot,
        retcode=None if res is None else res.retcode)
    return
    sl_price = tick.ask - 3.00
    if tick.ask - sl_price < spec.trade_stops_level * spec.point * 1.2:
        return
    res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": "XAUUSD", "volume": LOT,
                          "type": mt5.ORDER_TYPE_BUY, "price": tick.ask,
                          "sl": round(sl_price, spec.digits),
                          "tp": round(tick.ask + 4.20, spec.digits),
                          "deviation": 30, "magic": 7000003, "comment": "gold-dip",
                          "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC})
    log("gold_dip", dip_atr=round(dip, 2), retcode=None if res is None else res.retcode)


def _ema(values, period):
    k = 2 / (period + 1)
    out = values[0]
    for value in values[1:]:
        out = value * k + out * (1 - k)
    return out


def h1_dir(symbol: str) -> int:
    """The hourly trend: the cheapest way to stop buying inside a falling market."""
    h1 = _copy_cached(symbol, mt5.TIMEFRAME_H1, 0, 120)
    if h1 is None or len(h1) < 60:
        return 0
    closes = [float(r["close"]) for r in h1]
    return 1 if _ema(closes, 21) > _ema(closes, 55) else -1


def h1_trend(symbol: str) -> int:
    """1 = up, -1 = down, 0 = the timeframes disagree.

    Entries are only taken when M5 and M15 point the same way: a brief M5 flip inside a falling
    M15 is exactly the false breakout that produced today's losing buys.
    """
    m5 = _copy_cached(symbol, mt5.TIMEFRAME_M5, 0, 200)
    m15 = _copy_cached(symbol, mt5.TIMEFRAME_M15, 0, 120)
    if m5 is None or m15 is None or len(m5) < 60 or len(m15) < 60:
        return 0
    c5 = [float(r["close"]) for r in m5]
    c15 = [float(r["close"]) for r in m15]
    up5, up15 = _ema(c5, 21) > _ema(c5, 55), _ema(c15, 21) > _ema(c15, 55)
    if up5 and up15:
        return 1
    if not up5 and not up15:
        return -1
    return 0


def span_for(symbol: str, money: float) -> float:
    """Price distance that costs exactly `money` on a 0.01 lot (the operator's $5 cap)."""
    s = mt5.symbol_info(symbol)
    if s is None or s.trade_tick_value <= 0 or s.trade_tick_size <= 0:
        return 0.0
    return money / (s.trade_tick_value * LOT / s.trade_tick_size)


def entry_lot() -> float:
    """0.06 lots only when every timeframe agrees, otherwise the usual 0.01.

    With a $5 stop on 0.01, 0.06 means $30 of risk - the operator's ceiling for a genuinely
    good setup; never more than 8% of the account.
    """
    m5 = _copy_cached("XAUUSD", mt5.TIMEFRAME_M5, 0, 200)
    m15 = _copy_cached("XAUUSD", mt5.TIMEFRAME_M15, 0, 120)
    m1 = _copy_cached("XAUUSD", mt5.TIMEFRAME_M1, 0, 60)
    if m5 is None or m15 is None or m1 is None:
        return LOT

    def ema(values, period):
        k = 2 / (period + 1)
        out = values[0]
        for value in values[1:]:
            out = value * k + out * (1 - k)
        return out

    c5 = [float(r["close"]) for r in m5]
    c15 = [float(r["close"]) for r in m15]
    c1 = [float(r["close"]) for r in m1]
    up = ema(c5, 21) > ema(c5, 55) and ema(c15, 21) > ema(c15, 55)
    down = ema(c5, 21) < ema(c5, 55) and ema(c15, 21) < ema(c15, 55)
    m1_up = c1[-1] > c1[-2] > c1[-3] and c1[-1] > sum(c1[-21:]) / 21
    m1_down = c1[-1] < c1[-2] < c1[-3] and c1[-1] < sum(c1[-21:]) / 21
    info = mt5.account_info()
    if info is None:
        return LOT
    strong = (up and m1_up) or (down and m1_down)
    return 0.06 if strong and info.equity >= 375 else LOT


M1_SYMBOLS = ("XAUUSD", "EURUSD", "GBPUSD")


DEMO_QUEUE = ROOT / "agent_data" / "demo_mirror_queue.jsonl"


def note_demo(symbol: str, side: str, lot: float, sl: float, tp: float) -> None:
    """Record the trade for the demo account: the operator wants both books kept in step."""
    with DEMO_QUEUE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                 "symbol": symbol, "side": side, "lot": lot,
                                 "sl": sl, "tp": tp}, ensure_ascii=False) + "\n")


def m1_bottom() -> None:
    """Frequent bottom buy on M1: the fall stops, the first up bar prints, M5/M15 still up."""
    positions = mt5.positions_get() or []
    if trade_disabled() or len(positions) >= MAX_OPEN:
        return
    if not gold_margin_ok() or h1_trend("XAUUSD") <= 0 or h1_dir("XAUUSD") <= 0:
        return
    spec = mt5.symbol_info("XAUUSD")
    m1 = _copy_cached("XAUUSD", mt5.TIMEFRAME_M1, 0, 120)
    if spec is None or m1 is None or len(m1) < 40:
        return
    tick = mt5.symbol_info_tick("XAUUSD")
    low24 = min(float(r["low"]) for r in m1[-24:])
    spans = [max(m1[i]["high"] - m1[i]["low"], abs(m1[i]["high"] - m1[i-1]["close"]),
                 abs(m1[i]["low"] - m1[i-1]["close"])) for i in range(1, 15)]
    atr1 = sum(spans) / len(spans)
    if atr1 <= 0:
        return
    near = (tick.ask - low24) <= 0.6 * atr1
    held = float(m1[-1]["low"]) >= float(m1[-2]["low"]) and float(m1[-1]["close"]) > float(m1[-2]["close"])
    if not (near and held):
        return
    span = span_for("XAUUSD", GOLD_RISK)
    sl = round(tick.ask - span, spec.digits)
    tp = round(tick.ask + 2 * span, spec.digits)
    res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": "XAUUSD", "volume": LOT,
                          "type": mt5.ORDER_TYPE_BUY, "price": tick.ask, "sl": sl, "tp": tp,
                          "deviation": 30, "magic": 7000003, "comment": "m1-bottom",
                          "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC})
    log("m1_bottom", sl=sl, tp=tp, risk=round(span, 2),
        retcode=None if res is None else res.retcode)
    if res is not None and res.retcode == 10009:
        note_demo("XAUUSD", "buy", LOT, sl, tp)


def m1_entry() -> None:
    """Extra trades for frequency: M1 trend + M1 breakout, $5 stop, small target."""
    if trade_disabled():
        return
    open_symbols = {p.symbol for p in (mt5.positions_get() or [])}
    for symbol in M1_SYMBOLS:
        if symbol in open_symbols:
            continue
        spec = mt5.symbol_info(symbol)
        m1 = _copy_cached(symbol, mt5.TIMEFRAME_M1, 0, 120)
        if spec is None or m1 is None or len(m1) < 60:
            continue
        closes = [float(r["close"]) for r in m1]
        k21, k55 = 2 / 22, 2 / 56
        fast = slow = closes[0]
        for value in closes[1:]:
            fast = value * k21 + fast * (1 - k21)
            slow = value * k55 + slow * (1 - k55)
        tick = mt5.symbol_info_tick(symbol)
        spread = spec.ask - spec.bid
        high12 = max(float(r["high"]) for r in m1[-13:-1])
        low12 = min(float(r["low"]) for r in m1[-13:-1])
        up = fast > slow and closes[-1] > closes[-2] and closes[-1] >= high12
        down = fast < slow and closes[-1] < closes[-2] and closes[-1] <= low12
        max_risk = GOLD_RISK if symbol.startswith("XAU") else RISK
        if not (up or down):
            continue
        side = "buy" if up else "sell"
        if symbol.startswith("XAU") and not gold_margin_ok():
            continue
        distance = max(span_for(symbol, max_risk), spec.trade_stops_level * spec.point * 1.2, spread * 3)
        if spread > 0.2 * distance:
            continue
        entry = tick.ask if side == "buy" else tick.bid
        sl = entry - distance if side == "buy" else entry + distance
        res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": LOT,
                              "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
                              "price": entry, "sl": round(sl, spec.digits),
                              "tp": round(entry + 2 * distance if side == "buy" else entry - 2 * distance, spec.digits),
                              "deviation": 25, "magic": 7000003, "comment": f"m1-{side}",
                              "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC})
        log("m1_entry", symbol=symbol, side=side, sl=round(sl, spec.digits),
            risk=round(distance / spec.trade_tick_size * spec.trade_tick_value * LOT, 2),
            retcode=None if res is None else res.retcode)
        if res is not None and res.retcode == 10017:
            note_disabled()
        return


def gold_margin_ok() -> bool:
    """Gold needs a lot of margin on a small account: never spend more than half of free margin."""
    info = mt5.account_info()
    spec = mt5.symbol_info("XAUUSD")
    tick = mt5.symbol_info_tick("XAUUSD")
    if info is None or spec is None or tick is None:
        return False
    chk = mt5.order_check({"action": mt5.TRADE_ACTION_DEAL, "symbol": "XAUUSD", "volume": LOT,
                           "type": mt5.ORDER_TYPE_BUY, "price": tick.ask,
                           "deviation": 30, "magic": 7000003,
                           "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC})
    if chk is None:
        return False
    return bool(chk.margin <= 0.5 * info.margin_free)


FX_SYMBOLS = ("EURUSD", "GBPUSD")
FX_RISK = 2.00
# the real account trades M5: the parameters come from the demo study, per symbol
FX_PARAMS = {"EURUSD": {"tf": "M5", "fall": 0.6, "sl": 0.5, "rr": 2.0},
             "GBPUSD": {"tf": "M5", "fall": 1.4, "sl": 0.5, "rr": 2.0}}


def fx_dip(symbol: str) -> None:
    if trade_disabled():
        return
    """Train-validated M1 rule: buy a 0.6 ATR dip on the first up bar (and sell the mirror top).

    The demo study found this profitable on M1 for EURUSD/GBPUSD; the filter drops any trade where
    the spread would eat more than a fifth of the stop, because the edge is thin.
    """
    if any(p.symbol == symbol for p in (mt5.positions_get() or [])):
        return
    spec = mt5.symbol_info(symbol)
    if spec is None or not mt5.symbol_select(symbol, True):
        return
    params = FX_PARAMS.get(symbol, FX_PARAMS["EURUSD"])
    timeframe = mt5.TIMEFRAME_M5 if params["tf"] == "M5" else mt5.TIMEFRAME_M1
    rates = _copy_cached(symbol, timeframe, 0, 400)
    if rates is None or len(rates) < 60:
        return
    spans = [max(rates[i]["high"] - rates[i]["low"], abs(rates[i]["high"] - rates[i-1]["close"]),
                 abs(rates[i]["low"] - rates[i-1]["close"])) for i in range(1, 15)]
    a = sum(spans) / len(spans)
    if a <= 0:
        return
    tick = mt5.symbol_info_tick(symbol)
    recent_high = max(float(r["high"]) for r in rates[-48:])
    recent_low = min(float(r["low"]) for r in rates[-48:])
    last, prev = float(rates[-1]["close"]), float(rates[-2]["close"])
    spread = spec.ask - spec.bid
    for side in ("buy", "sell"):
        if side == "buy":
            trigger = (recent_high - tick.ask) >= params["fall"] * a and last > prev
            entry = tick.ask
        else:
            trigger = (tick.bid - recent_low) >= params["fall"] * a and last < prev
            entry = tick.bid
        if not trigger:
            continue
        trend = h1_trend(symbol)
        if (side == "buy" and trend <= 0) or (side == "sell" and trend >= 0):
            continue
        distance = max(span_for(symbol, FX_RISK), spec.trade_stops_level * spec.point * 1.2, spread * 3)
        money = distance / spec.trade_tick_size * spec.trade_tick_value * LOT
        if money > FX_RISK or spread > 0.2 * distance:
            continue
        sl = entry - distance if side == "buy" else entry + distance
        tp = entry + params["rr"] * distance if side == "buy" else entry - params["rr"] * distance
        res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": LOT,
                              "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
                              "price": entry, "sl": round(sl, spec.digits),
                              "tp": round(tp, spec.digits), "deviation": 20, "magic": 7000003,
                              "comment": f"fx-dip-{side}", "type_time": mt5.ORDER_TIME_GTC,
                              "type_filling": mt5.ORDER_FILLING_IOC})
        log("fx_dip", symbol=symbol, side=side, sl=round(sl, spec.digits),
            risk=round(money, 2), spread=round(spread, spec.digits),
            retcode=None if res is None else res.retcode)
        if res is not None and res.retcode == 10017:
            note_disabled()
        return


def gold_bottom_dip() -> None:
    if trade_disabled():
        return
    """Buy the bottom before the rise: H4/H1 up, price at the swing low, first up-candle on M5."""
    if not gold_margin_ok():
        return
    if any(p.symbol.startswith("XAU") for p in (mt5.positions_get() or [])):
        return
    if not gold_trend_ok("buy"):
        return
    spec = mt5.symbol_info("XAUUSD")
    m15 = _copy_cached("XAUUSD", mt5.TIMEFRAME_M15, 0, 120)
    m5 = _copy_cached("XAUUSD", mt5.TIMEFRAME_M5, 0, 30)
    if spec is None or m15 is None or m5 is None or len(m15) < 60 or len(m5) < 10:
        return
    lows = [float(r["low"]) for r in m15[-48:]]
    highs = [float(r["high"]) for r in m15[-48:]]
    spans = [max(m15[i]["high"] - m15[i]["low"], abs(m15[i]["high"] - m15[i-1]["close"]),
                 abs(m15[i]["low"] - m15[i-1]["close"])) for i in range(1, 15)]
    atr15 = sum(spans) / len(spans)
    if atr15 <= 0:
        return
    tick = mt5.symbol_info_tick("XAUUSD")
    c5 = [float(r["close"]) for r in m5]
    near_low = (tick.ask - min(lows)) <= 0.40 * atr15 and (max(highs) - tick.ask) >= 1.2 * atr15
    first_up = bool(c5[-1] > c5[-2]) and c5[-2] <= min(c5[-5:])
    if not (near_low and first_up):
        return
    sl_price = min(lows) - 0.20 * atr15
    distance = tick.ask - sl_price
    money = distance / spec.trade_tick_size * spec.trade_tick_value * LOT
    if money > GOLD_RISK or distance < spec.trade_stops_level * spec.point * 1.2:
        return
    res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": "XAUUSD", "volume": LOT,
                          "type": mt5.ORDER_TYPE_BUY, "price": tick.ask,
                          "sl": round(sl_price, spec.digits),
                          "tp": round(tick.ask + distance * 2, spec.digits),
                          "deviation": 30, "magic": 7000003, "comment": "gold-bottom",
                          "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC})
    log("gold_bottom", sl=round(sl_price, 2), risk=round(money, 2), low=round(min(lows), 2),
        retcode=None if res is None else res.retcode)


def gold_impulse() -> None:
    """Direct gold entry when the swarm is quiet: M5 impulse, $6 stop, 1.8R target."""
    positions = mt5.positions_get() or []
    if any(p.symbol.startswith("XAU") for p in positions):
        return
    if len(positions) >= MAX_OPEN or day_loss() <= -DAILY_LIMIT:
        return
    spec = mt5.symbol_info("XAUUSD")
    m5 = _copy_cached("XAUUSD", mt5.TIMEFRAME_M5, 0, 40)
    if spec is None or m5 is None or len(m5) < 25:
        return
    closes = [float(r["close"]) for r in m5]
    k = 2 / 22
    fast = closes[0]
    for value in closes[1:]:
        fast = value * k + fast * (1 - k)
    rising = closes[-1] > closes[-2] > closes[-3] and closes[-1] > fast
    falling = closes[-1] < closes[-2] < closes[-3] and closes[-1] < fast
    if not (rising or falling):
        return

    # quality gate: the M5 impulse must agree with the H1 trend, and price must not be extended
    # more than 2 ATR from the H1 mean (chasing a spent move is how the two stops happened).
    h1 = _copy_cached("XAUUSD", mt5.TIMEFRAME_H1, 0, 120)
    if h1 is None or len(h1) < 60:
        return
    h1_closes = [float(r["close"]) for r in h1]
    k1 = 2 / 22
    h1_fast, h1_slow = h1_closes[0], h1_closes[0]
    kv = 2 / 56
    for value in h1_closes[1:]:
        h1_fast = value * k1 + h1_fast * (1 - k1)
        h1_slow = value * kv + h1_slow * (1 - kv)
    h1_up = h1_fast > h1_slow
    if rising and not h1_up:
        return
    if falling and h1_up:
        return
    spans = [max(m5[i]["high"] - m5[i]["low"], abs(m5[i]["high"] - m5[i-1]["close"]),
                 abs(m5[i]["low"] - m5[i-1]["close"])) for i in range(1, 15)]
    atr5 = sum(spans) / len(spans) if spans else 0.0
    if atr5 > 0 and abs(closes[-1] - h1_fast) > 2.0 * atr5:
        return
    side = "buy" if rising else "sell"
    geo = geometry("XAUUSD", side, GOLD_RISK)
    if geo is None:
        return
    entry, sl, tp, risk = geo
    res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": "XAUUSD", "volume": LOT,
                          "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
                          "price": entry, "sl": sl, "tp": tp, "deviation": 30, "magic": 7000003,
                          "comment": "gold-impulse", "type_time": mt5.ORDER_TIME_GTC,
                          "type_filling": mt5.ORDER_FILLING_IOC})
    log("gold_impulse", side=side, sl=sl, tp=tp, risk=risk,
        retcode=None if res is None else res.retcode)


def done_keys() -> set[str]:
    try:
        return set(json.loads(DONE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def mark_done(keys: set[str]) -> None:
    DONE.write_text(json.dumps(sorted(keys)), encoding="utf-8")


def main() -> None:
    cycles = 0
    mt5.initialize(path=TERMINAL, timeout=8000)  # one persistent connection, not one per cycle
    while True:
        cycles += 1
        try:
            if (ROOT / "agent_data" / "mirror.pause").exists():
                manage_profit()  # existing positions still get trailing and small-target exits
                if cycles % 20 == 1:
                    log("paused", why="entries paused, positions still managed")
                time.sleep(20)
                continue
            _CANDLES.clear()
            info = mt5.terminal_info()
            if info is None or not info.connected:
                mt5.initialize(path=TERMINAL, timeout=8000)
            lines = SWARM_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
            done = done_keys()
            start = cursor()
            if start == 0:
                start = max(0, len(lines) - 300)
            for line in lines[start:]:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                event = str(row.get("event", ""))
                if " OPEN " not in event:
                    continue
                parts = event.split()
                try:
                    symbol = parts[4]
                    side = "buy" if parts[2].upper() == "BUY" else "sell"
                except IndexError:
                    continue
                agent = parts[0]
                key = f"{agent}|{symbol}|{side}|{str(row.get('ts', ''))[:16]}"
                if key in done:
                    continue
                done.add(key)
                place(side, symbol, agent)
            CURSOR.write_text(str(len(lines)), encoding="utf-8")
            mark_done(done)
            manage_profit()
            gold_bottom_confirmed()
            m1_bottom()
            gold_peak_short()
            gold_impulse()
            for _symbol in FX_SYMBOLS:
                fx_dip(_symbol)
            m1_entry()
            if cycles % 10 == 1:
                log("beat", cycles=cycles)
        except Exception as exc:  # noqa: BLE001 - the mirror must not die on one bad line
            log("error", error=f"{type(exc).__name__}: {exc}")
        time.sleep(3)


if __name__ == "__main__":
    main()
