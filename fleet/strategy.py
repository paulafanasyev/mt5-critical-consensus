"""Strategies: read bars, return a signal; judge exits from the position's own levels.

Exits are measured from the ATR that was on the chart when the trade was opened — the stop and
target the broker actually holds — not from the live ATR, which would silently move the target and
the stop every hour. Real accounts take profit earlier and give back less than demo ones.
"""
from __future__ import annotations

import dataclasses

import MetaTrader5 as mt5

from . import broker
from .config import Account


@dataclasses.dataclass(slots=True)
class Signal:
    base_symbol: str
    side: str
    reason: str
    price: float
    stop: float
    target: float
    atr: float

    @property
    def stop_distance(self) -> float:
        return abs(self.price - self.stop)


def _trend(name: str) -> str:
    h4_fast = broker.ema(name, mt5.TIMEFRAME_H4, 21)
    h4_slow = broker.ema(name, mt5.TIMEFRAME_H4, 55)
    h1_fast = broker.ema(name, mt5.TIMEFRAME_H1, 21)
    h1_slow = broker.ema(name, mt5.TIMEFRAME_H1, 55)
    if min(h4_fast, h4_slow, h1_fast, h1_slow) <= 0:
        return ""
    if h4_fast > h4_slow and h1_fast > h1_slow:
        return "up"
    if h4_fast < h4_slow and h1_fast < h1_slow:
        return "down"
    return ""


def _prices(name: str) -> tuple[float, float]:
    """(bid, ask) for one symbol in one read: the two sides have to come from the same tick."""
    tick = mt5.symbol_info_tick(name)
    if not tick:
        return 0.0, 0.0
    return float(tick.bid), float(tick.ask)


def _side_price(bid: float, ask: float, side: str) -> float:
    """The price this side can actually be filled at: a buy pays ask, a sell receives bid.

    Stamping both sides with the ask bills a sell the whole spread as if it were the cost of
    waiting for confirmation — at a still market the candidate book would charge a sell 30 points
    for nothing — and it puts a sell's stop one spread from the price that triggers it.
    """
    return ask if side.casefold() == "buy" else bid


def _passes_filters(account: Account, spec: broker.BrokerSymbol, atr: float,
                    price: float) -> bool:
    if not broker.spread_within_stop(spec, atr * account.risk.stop_atr,
                                     max_fraction=0.15):
        return False
    if price <= 0:
        return False
    if atr / price < account.strategy.min_atr_pct:
        return False
    return True


def trend_signal(account: Account, base_symbol: str) -> Signal | None:
    spec = broker.symbol(base_symbol)
    if spec is None:
        return None
    atr = broker.atr(spec.name, mt5.TIMEFRAME_H1)
    if atr <= 0:
        return None
    bid, ask = _prices(spec.name)
    if not _passes_filters(account, spec, atr, ask):
        return None
    direction = _trend(spec.name)
    if not direction:
        return None
    bars = mt5.copy_rates_from_pos(spec.name, mt5.TIMEFRAME_H1, 0, 3)
    if bars is None or len(bars) < 3:
        return None
    last_close = float(bars[-1]["close"])
    fast = broker.ema(spec.name, mt5.TIMEFRAME_H1, 21)
    floor = broker.min_level_distance(spec, ask - bid)
    stop_distance = max(atr * account.risk.stop_atr, floor)
    take_distance = max(atr * _take_atr(account), floor)
    if direction == "up":
        if last_close < fast:
            return None
        price = _side_price(bid, ask, "buy")
        return Signal(base_symbol, "buy", "H4+H1 тренд вверх, цена над EMA21",
                      price, price - stop_distance, price + take_distance, atr)
    if last_close > fast:
        return None
    price = _side_price(bid, ask, "sell")
    return Signal(base_symbol, "sell", "H4+H1 тренд вниз, цена под EMA21",
                  price, price + stop_distance, price - take_distance, atr)


def breakout_signal(account: Account, base_symbol: str, lookback: int = 24) -> Signal | None:
    spec = broker.symbol(base_symbol)
    if spec is None:
        return None
    bars = mt5.copy_rates_from_pos(spec.name, mt5.TIMEFRAME_H1, 0, lookback + 2)
    if bars is None or len(bars) < lookback + 2:
        return None
    atr = broker.atr(spec.name, mt5.TIMEFRAME_H1)
    bid, ask = _prices(spec.name)
    if not _passes_filters(account, spec, atr, ask):
        return None
    window = bars[-(lookback + 1):-1]
    high = max(float(row["high"]) for row in window)
    low = min(float(row["low"]) for row in window)
    close = float(bars[-1]["close"])
    direction = _trend(spec.name)
    floor = broker.min_level_distance(spec, ask - bid)
    stop_distance = max(atr * account.risk.stop_atr, floor)
    take_distance = max(atr * _take_atr(account), floor)
    if close > high and direction == "up":
        price = _side_price(bid, ask, "buy")
        return Signal(base_symbol, "buy", f"пробой {lookback}-часового максимума",
                      price, price - stop_distance, price + take_distance, atr)
    if close < low and direction == "down":
        price = _side_price(bid, ask, "sell")
        return Signal(base_symbol, "sell", f"пробой {lookback}-часового минимума",
                      price, price + stop_distance, price - take_distance, atr)
    return None


def pullback_signal(account: Account, base_symbol: str) -> Signal | None:
    """Ждём откат к EMA21 внутри тренда, а не гонимся за уехавшей ценой.

    «Тренд вверх, цена над EMA21» покупает на ноже: после резкого часа EMA21 отстаёт и цена ещё
    над ней, хотя весь день идёт вниз. Здесь вход только там, где текущий час уже коснулся EMA21 и
    вернулся на свою сторону линии: в downtrend час бил вверх по EMA21 и закрылся под ней — шорт;
    в uptrend час проваливался к EMA21 и вернулся над ней — лонг.
    """
    spec = broker.symbol(base_symbol)
    if spec is None:
        return None
    atr = broker.atr(spec.name, mt5.TIMEFRAME_H1)
    if atr <= 0:
        return None
    bid, ask = _prices(spec.name)
    if not _passes_filters(account, spec, atr, ask):
        return None
    direction = _trend(spec.name)
    if not direction:
        return None
    bars = mt5.copy_rates_from_pos(spec.name, mt5.TIMEFRAME_H1, 0, 2)
    if bars is None or len(bars) < 2:
        return None
    current = bars[-1]
    fast = broker.ema(spec.name, mt5.TIMEFRAME_H1, 21)
    floor = broker.min_level_distance(spec, ask - bid)
    stop_distance = max(atr * account.risk.stop_atr, floor)
    take_distance = max(atr * _take_atr(account), floor)
    if direction == "up":
        if float(current["low"]) > fast or float(current["close"]) <= fast:
            return None
        price = _side_price(bid, ask, "buy")
        return Signal(base_symbol, "buy", "тренд H4+H1 вверх, час откатился к EMA21 и вернулся над ней",
                      price, price - stop_distance, price + take_distance, atr)
    if float(current["high"]) < fast or float(current["close"]) >= fast:
        return None
    price = _side_price(bid, ask, "sell")
    return Signal(base_symbol, "sell", "тренд H4+H1 вниз, час откатился к EMA21 и вернулся под неё",
                  price, price + stop_distance, price - take_distance, atr)


KINDS = {"trend": trend_signal, "breakout": breakout_signal, "pullback": pullback_signal}


def signals(account: Account) -> list[Signal]:
    found: list[Signal] = []
    for base_symbol in account.symbols:
        for kind in account.strategy.kinds:
            if kind in ("trend", "pullback") and "H4" not in account.strategy.timeframes:
                continue
            builder = KINDS.get(kind)
            if builder is None:
                continue
            try:
                result = builder(account, base_symbol)
            except Exception:  # noqa: BLE001 - one bad instrument must not stop the account
                result = None
            if result is not None:
                found.append(result)
                break
    found.sort(key=lambda s: s.stop_distance / s.atr if s.atr else 99)
    return found


def _take_atr(account: Account) -> float:
    return (account.risk.take_atr_real if account.is_real else account.risk.take_atr)


def _giveback_fraction(account: Account) -> float:
    return (account.risk.giveback_fraction_real if account.is_real
            else account.risk.giveback_fraction)


def entry_distances(position, spec: broker.BrokerSymbol,
                    atr_entry: float, account: Account) -> tuple[float, float]:
    """Stop and target distances the trade was actually opened with."""
    entry = float(position.price_open)
    sl = float(getattr(position, "sl", 0.0) or 0.0)
    tp = float(getattr(position, "tp", 0.0) or 0.0)
    fallback_stop = atr_entry * account.risk.stop_atr if atr_entry > 0 else 0.0
    fallback_take = atr_entry * _take_atr(account) if atr_entry > 0 else 0.0
    stop_distance = abs(entry - sl) if sl > 0 else fallback_stop
    take_distance = abs(tp - entry) if tp > 0 else fallback_take
    if account.is_real and fallback_take > 0:
        take_distance = min(take_distance, fallback_take)
    return stop_distance, take_distance


def break_even_level(account: Account, position, spec: broker.BrokerSymbol,
                     atr_entry: float) -> float | None:
    """A stop at the entry price once the trade is far enough in profit to deserve protection."""
    if atr_entry <= 0:
        return None
    entry = float(position.price_open)
    is_buy = int(position.type) == int(mt5.POSITION_TYPE_BUY)
    tick = mt5.symbol_info_tick(spec.name)
    if tick is None:
        return None
    price = float(tick.bid if is_buy else tick.ask)
    gain = (price - entry) if is_buy else (entry - price)
    if gain < atr_entry * account.risk.break_even_atr:
        return None
    current = float(getattr(position, "sl", 0.0) or 0.0)
    spread = abs(float(tick.ask) - float(tick.bid))
    target = entry + spread if is_buy else entry - spread
    if is_buy and (current <= 0 or target > current):
        return target
    if not is_buy and (current <= 0 or target < current):
        return target
    return None


def exit_reason(account: Account, position, spec: broker.BrokerSymbol, peak: float,
                atr_entry: float) -> str:
    """The single exit decision: target, stop, protected giveback, or nothing."""
    tick = mt5.symbol_info_tick(spec.name)
    if tick is None:
        return ""
    is_buy = int(position.type) == int(mt5.POSITION_TYPE_BUY)
    entry = float(position.price_open)
    price = float(tick.bid if is_buy else tick.ask)
    gain = (price - entry) if is_buy else (entry - price)
    stop_distance, take_distance = entry_distances(position, spec, atr_entry, account)
    if take_distance > 0 and gain >= take_distance:
        return f"тейк {take_distance / max(atr_entry, 1e-9):.2f} ATR"
    if stop_distance > 0 and gain <= -stop_distance * 1.02:
        return f"стоп {stop_distance / max(atr_entry, 1e-9):.2f} ATR"
    if peak > 0 and gain > 0:
        floor = max(atr_entry * 0.25, _giveback_fraction(account) * peak)
        if gain <= peak - floor:
            return "фиксация после отката от пика"
    if account.risk.max_hold_minutes > 0:
        import time as _time

        server_now = broker.server_timestamp() or _time.time()
        held = (server_now - float(getattr(position, "time", 0))) / 60
        if held >= account.risk.max_hold_minutes:
            return f"время вышло ({held:.0f} мин)"
    return ""
