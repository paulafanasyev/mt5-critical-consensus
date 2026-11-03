"""Strategies: plain functions that read bars and return a signal, never touch orders.

Because the fleet trades hundreds of accounts, a strategy must be cheap, deterministic and
broker-agnostic: it works with the broker's resolved symbol name and returns target levels in
price terms. The worker turns that into volume and an order.
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
    """Return 'up', 'down' or '' from the H4 and H1 averages on one symbol."""
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


def _price(name: str) -> float:
    tick = mt5.symbol_info_tick(name)
    return float(tick.ask) if tick else 0.0


def trend_signal(account: Account, base_symbol: str) -> Signal | None:
    """Buy with the H4+H1 trend after a pullback close back above the fast H1 average."""
    spec = broker.symbol(base_symbol)
    if spec is None:
        return None
    if not broker.spread_ok(spec, account.risk.max_spread_points):
        return None
    direction = _trend(spec.name)
    if not direction:
        return None
    atr = broker.atr(spec.name, mt5.TIMEFRAME_H1)
    if atr <= 0:
        return None
    bars = mt5.copy_rates_from_pos(spec.name, mt5.TIMEFRAME_H1, 0, 3)
    if bars is None or len(bars) < 3:
        return None
    last_close = float(bars[-1]["close"])
    fast = broker.ema(spec.name, mt5.TIMEFRAME_H1, 21)
    price = _price(spec.name)
    if price <= 0:
        return None
    stop_distance = atr * account.risk.stop_atr
    if direction == "up":
        if last_close < fast:  # only enter when momentum agrees with the trend
            return None
        stop = price - stop_distance
        target = price + atr * account.risk.take_atr
        return Signal(base_symbol, "buy", "H4+H1 тренд вверх, цена над EMA21",
                      price, stop, target, atr)
    if last_close > fast:
        return None
    stop = price + stop_distance
    target = price - atr * account.risk.take_atr
    return Signal(base_symbol, "sell", "H4+H1 тренд вниз, цена под EMA21",
                  price, stop, target, atr)


def breakout_signal(account: Account, base_symbol: str, lookback: int = 24) -> Signal | None:
    """Trade an H1 close outside the last N bars, but only with the higher-timeframe trend."""
    spec = broker.symbol(base_symbol)
    if spec is None or not broker.spread_ok(spec, account.risk.max_spread_points):
        return None
    bars = mt5.copy_rates_from_pos(spec.name, mt5.TIMEFRAME_H1, 0, lookback + 2)
    if bars is None or len(bars) < lookback + 2:
        return None
    window = bars[-(lookback + 1):-1]
    high = max(float(row["high"]) for row in window)
    low = min(float(row["low"]) for row in window)
    close = float(bars[-1]["close"])
    atr = broker.atr(spec.name, mt5.TIMEFRAME_H1)
    if atr <= 0:
        return None
    direction = _trend(spec.name)
    price = _price(spec.name)
    if price <= 0:
        return None
    stop_distance = atr * account.risk.stop_atr
    if close > high and direction == "up":
        return Signal(base_symbol, "buy", f"пробой {lookback}-часового максимума",
                      price, price - stop_distance, price + atr * account.risk.take_atr, atr)
    if close < low and direction == "down":
        return Signal(base_symbol, "sell", f"пробой {lookback}-часового минимума",
                      price, price + stop_distance, price - atr * account.risk.take_atr, atr)
    return None


KINDS = {
    "trend": trend_signal,
    "breakout": breakout_signal,
}


def signals(account: Account) -> list[Signal]:
    """All actionable signals for one account, best first."""
    found: list[Signal] = []
    for base_symbol in account.symbols:
        for kind in account.strategy.kinds:
            builder = KINDS.get(kind)
            if builder is None:
                continue
            try:
                result = builder(account, base_symbol)
            except Exception:  # noqa: BLE001 - one bad instrument must not stop the account
                result = None
            if result is not None:
                found.append(result)
                break  # one signal per instrument
    found.sort(key=lambda s: s.atr and (s.stop_distance / s.atr) or 99)
    return found


def exit_reason(account: Account, position, spec: broker.BrokerSymbol,
                peak: float) -> str:
    """Decide whether an open position should be closed now.

    This is the single place that decides exits — the worker only executes what comes back. The
    operator's standing rules: take profit earlier on real money, never let a winner turn into a
    loser, and never hold past the planned horizon.
    """
    atr = broker.atr(spec.name, mt5.TIMEFRAME_H1)
    if atr <= 0:
        return ""
    tick = mt5.symbol_info_tick(spec.name)
    if tick is None:
        return ""
    is_buy = position.type == mt5.POSITION_TYPE_BUY
    price = float(tick.bid if is_buy else tick.ask)
    entry = float(position.price_open)
    gain = (price - entry) if is_buy else (entry - price)

    if gain >= atr * account.risk.take_atr:
        return "тейк по ATR"
    if gain <= -atr * account.risk.stop_atr * 1.05:
        return "жёсткий стоп по ATR"
    if gain >= atr * account.risk.trail_arm_atr and peak > 0:
        giveback_floor = max(atr * 0.35, account.risk.giveback_fraction * peak)
        if gain <= peak - giveback_floor:
            return "фиксация после отката от пика"
    if account.risk.max_hold_minutes > 0:
        import time as _time

        held_minutes = (_time.time() - float(position.time)) / 60
        if held_minutes >= account.risk.max_hold_minutes:
            return f"время вышло ({held_minutes:.0f} мин)"
    return ""
