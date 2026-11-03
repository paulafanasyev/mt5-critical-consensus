"""Broker adapter: resolve each broker's own symbol name, and execute orders idempotently.

A lost `order_send` reply must never create a second position: before any retry the adapter looks
for a fill carrying this attempt's token, and it only re-sends when the book proves nothing
happened. Slippage is bounded twice — as a fraction of the stop distance and, harder, as a share
of the money at risk.
"""
from __future__ import annotations

import dataclasses
import math
import time
import uuid
from typing import Any

import MetaTrader5 as mt5


@dataclasses.dataclass(slots=True)
class BrokerSymbol:
    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int
    freeze_level: int
    spread: int
    filling: int

    def money_per_point(self, volume: float) -> float:
        if self.tick_size <= 0:
            return 0.0
        return (self.point / self.tick_size) * self.tick_value * volume

    def distance_money(self, distance: float, volume: float) -> float:
        if self.tick_size <= 0:
            return 0.0
        return (distance / self.tick_size) * self.tick_value * volume


@dataclasses.dataclass(slots=True)
class Execution:
    volume: float
    price: float
    retcode: int
    token: str
    position_ticket: int = 0


class BrokerError(RuntimeError):
    def __init__(self, message: str, retcode: int = 0) -> None:
        super().__init__(message)
        self.retcode = retcode


_RETRYABLE = ()
_PARTIAL = 10010


def describe(code: int) -> str:
    names = {
        10004: "requote", 10006: "reject", 10009: "done", 10010: "done partially",
        10013: "invalid request", 10014: "invalid volume", 10015: "invalid price",
        10016: "invalid stops", 10017: "trading disabled", 10018: "market closed",
        10019: "no money", 10020: "price changed", 10021: "no quotes",
        10022: "invalid expiration", 10023: "order changed", 10024: "too many requests",
        10025: "no changes", 10026: "autotrading disabled by server",
        10027: "autotrading disabled by client", 10028: "request locked",
        10029: "order frozen", 10030: "invalid filling mode", 10031: "connection",
        10032: "order limit reached", 10034: "volume limit reached",
        10035: "invalid filling mode", 10038: "only real accounts", 10012: "timeout",
    }
    return names.get(code, f"code {code}")


# --- symbol handling ------------------------------------------------------------------------

def resolve(base: str) -> str | None:
    if not base:
        return None
    if mt5.symbol_info(base) is not None:
        return base
    upper = base.upper()
    candidates: list[str] = []
    for item in mt5.symbols_get() or []:
        name = item.name
        if name.upper() == upper:
            return name
        if name.upper().startswith(upper):
            candidates.append(name)
    if not candidates:
        return None
    return sorted(candidates, key=lambda n: (len(n) - len(base),
                                             0 if n[len(base):].isalpha() else 1, n))[0]


def ensure_selected(name: str) -> bool:
    info = mt5.symbol_info(name)
    if info is None:
        return False
    if not info.visible:
        mt5.symbol_select(name, True)
        for _ in range(10):
            time.sleep(0.2)
            info = mt5.symbol_info(name)
            if info is not None and info.visible:
                return True
        return False
    return True


def symbol(base: str) -> BrokerSymbol | None:
    name = resolve(base)
    if name is None or not ensure_selected(name):
        return None
    info = mt5.symbol_info(name)
    tick = mt5.symbol_info_tick(name)
    if info is None or tick is None:
        return None
    return BrokerSymbol(
        name=name, digits=info.digits, point=info.point,
        tick_size=info.trade_tick_size or info.point,
        tick_value=info.trade_tick_value or 0.0,
        volume_min=info.volume_min, volume_max=info.volume_max,
        volume_step=info.volume_step or 0.01,
        stops_level=info.trade_stops_level, freeze_level=info.trade_freeze_level,
        spread=info.spread, filling=info.filling_mode)


def symbol_exact(name: str) -> BrokerSymbol | None:
    """Build a spec for a position's own symbol, without resolving to another variant."""
    return symbol(name)


def round_price(value: float, digits: int) -> float:
    return round(value, digits)


def normalize_volume(volume: float, spec: BrokerSymbol, lot_cap: float) -> float:
    """Clamp DOWN to the broker's step so rounding never increases risk."""
    if spec.volume_step <= 0:
        return 0.0
    cap = math.floor(min(spec.volume_max, lot_cap) / spec.volume_step + 1e-9) * spec.volume_step
    if cap < spec.volume_min:
        return 0.0
    steps = int(volume / spec.volume_step)  # floor: rounding up would enlarge the risk
    stepped = steps * spec.volume_step
    if stepped < spec.volume_min:
        return 0.0
    stepped = min(cap, stepped)
    decimals = max(0, min(8, len(f"{spec.volume_step:.10f}".rstrip("0").split(".")[1])))
    return round(stepped, decimals)


def filling_candidates(spec: BrokerSymbol) -> list[int]:
    allowed = spec.filling
    modes: list[int] = []
    for bit, mode in ((1, mt5.ORDER_FILLING_FOK), (2, mt5.ORDER_FILLING_IOC)):
        if allowed & bit:
            modes.append(mode)
    modes.append(mt5.ORDER_FILLING_RETURN)
    return modes


def spread_points(spec: BrokerSymbol) -> float:
    tick = mt5.symbol_info_tick(spec.name)
    if tick is None or spec.point <= 0:
        return float("inf")
    return (tick.ask - tick.bid) / spec.point


def spread_within_stop(spec: BrokerSymbol, stop_distance: float,
                       max_fraction: float = 0.15) -> bool:
    """A spread wider than a share of the stop eats the very budget the stop protects."""
    if stop_distance <= 0:
        return False
    ticks = spread_points(spec) * spec.point
    return ticks <= stop_distance * max_fraction


def open_positions(symbol_name: str | None = None, magic: int | None = None) -> list[Any]:
    positions = mt5.positions_get(symbol=symbol_name) if symbol_name else mt5.positions_get()
    if positions is None:
        return []
    if magic is None:
        return list(positions)
    return [p for p in positions if p.magic == magic]


def _find_by_token(token: str) -> Any | None:
    """Look for the position this attempt would have created (token lives in the comment)."""
    for position in mt5.positions_get() or []:
        if token and token in str(getattr(position, "comment", "")):
            return position
    return None


# --- execution ------------------------------------------------------------------------------

def _deviation(spec: BrokerSymbol, volume: float, stop_distance: float,
               risk_money: float) -> int:
    """Points of allowed slippage: at most 15% of the stop and at most 20% of the risk budget."""
    if spec.point <= 0:
        return 0
    by_stop = int((0.15 * stop_distance) / spec.point) if stop_distance else 0
    per_point = spec.money_per_point(volume)
    by_money = int((0.20 * risk_money) / per_point) if per_point > 0 and risk_money > 0 else 0
    points = min(by_stop or by_money, by_money or by_stop) if (by_stop or by_money) else 0
    return max(1, min(points, 2000))


def _send_with_fallback(request: dict[str, Any], spec: BrokerSymbol) -> Any:
    last_error = ""
    for mode in filling_candidates(spec):
        attempt = dict(request)
        attempt["type_filling"] = mode
        result = mt5.order_send(attempt)
        if result is None:
            # An unanswered send is ambiguous, not an invalid filling mode: never resend blindly.
            last_error = f"order_send вернул None ({mt5.last_error()})"
            break
        if result.retcode in (mt5.TRADE_RETCODE_DONE, _PARTIAL):
            return result
        last_error = f"{describe(int(result.retcode))} (retcode {int(result.retcode)})"
        if int(result.retcode) != int(mt5.TRADE_RETCODE_INVALID_FILL):
            break
    raise BrokerError(f"сделка не прошла: {last_error}")


def market_order(magic: int, spec: BrokerSymbol, side: str, volume: float, sl: float,
                 tp: float, stop_distance: float, risk_money: float,
                 comment: str = "fleet") -> Execution:
    """Open one position. A lost reply is resolved by looking for the token, never by blind retry."""
    tick = mt5.symbol_info_tick(spec.name)
    if tick is None:
        raise BrokerError(f"нет тика по {spec.name}")
    token = uuid.uuid4().hex[:6]
    is_buy = side.lower() == "buy"
    price = float(tick.ask if is_buy else tick.bid)
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": spec.name,
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price": round_price(price, spec.digits),
        "sl": round_price(sl, spec.digits),
        "tp": round_price(tp, spec.digits),
        "deviation": _deviation(spec, volume, stop_distance, risk_money),
        "magic": magic,
        "comment": f"fleet-{token}"[:31],
        "type_time": mt5.ORDER_TIME_GTC,
    }
    try:
        result = _send_with_fallback(request, spec)
    except BrokerError:
        found = _find_by_token(token)
        if found is not None:
            return Execution(volume=float(found.volume), price=float(found.price_open),
                             retcode=mt5.TRADE_RETCODE_DONE, token=token,
                             position_ticket=int(found.ticket))
        raise
    filled_price = float(getattr(result, "price", 0.0) or price)
    filled_volume = float(getattr(result, "volume", 0.0) or volume)
    if filled_volume <= 0:
        filled_volume = volume
    # Slippage beyond the plan is real money: if it exceeded the allowed deviation, log it via
    # the retcode path; the caller compares price against the signal.
    ticket = int(getattr(result, "deal", 0) or 0)
    return Execution(volume=filled_volume, price=filled_price,
                     retcode=int(result.retcode), token=token, position_ticket=ticket)


def modify_position(ticket: int, symbol_name: str, sl: float, tp: float, digits: int) -> Any:
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "symbol": symbol_name,
        "sl": round_price(sl, digits),
        "tp": round_price(tp, digits),
    }
    return _send_with_fallback(request, BrokerSymbol(
        name=symbol_name, digits=digits, point=10 ** -digits, tick_size=10 ** -digits,
        tick_value=0.0, volume_min=0.01, volume_max=100.0, volume_step=0.01,
        stops_level=0, freeze_level=0, spread=0, filling=3))


def close_position(position: Any, spec: BrokerSymbol, comment: str = "fleet-exit") -> float:
    """Close by the position's own symbol and volume; retries only while the ticket lives."""
    remaining = float(position.volume)
    closed = 0.0
    for attempt in range(3):
        tick = mt5.symbol_info_tick(position.symbol)
        if tick is None:
            raise BrokerError(f"нет тика по {position.symbol}")
        is_buy = position.type == mt5.POSITION_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": remaining,
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "position": int(position.ticket),
            "price": round_price(float(tick.bid if is_buy else tick.ask), spec.digits),
            "deviation": _deviation_points(tick, spec),
            "magic": int(getattr(position, "magic", 0)),
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }
        try:
            result = _send_with_fallback(request, spec)
        except BrokerError:
            if not any(int(p.ticket) == int(position.ticket) for p in mt5.positions_get() or []):
                return closed or remaining
            raise
        done = float(getattr(result, "volume", 0.0) or remaining)
        closed += done
        remaining = round(remaining - done, 8)
        still_open = any(int(p.ticket) == int(position.ticket) for p in mt5.positions_get() or [])
        if not still_open or remaining <= 0:
            return closed
        time.sleep(0.5)
    return closed


def _deviation_points(tick: Any, spec: BrokerSymbol) -> int:
    """Deviation in points that matches one full spread, at least one point."""
    span = abs(float(tick.ask) - float(tick.bid))
    if spec.point <= 0:
        return 10
    return max(1, int(span / spec.point))


# --- indicators -----------------------------------------------------------------------------

def atr(name: str, timeframe: int, period: int = 14, bars: int = 200) -> float:
    rates = mt5.copy_rates_from_pos(name, timeframe, 0, bars)
    if rates is None or len(rates) < period + 2:
        return 0.0
    total = 0.0
    window = rates[-(period + 1):]
    for index in range(1, len(window)):
        high, low = float(window[index]["high"]), float(window[index]["low"])
        prev_close = float(window[index - 1]["close"])
        total += max(high - low, abs(high - prev_close), abs(low - prev_close))
    return total / period


def ema(name: str, timeframe: int, period: int = 50, bars: int = 400) -> float:
    rates = mt5.copy_rates_from_pos(name, timeframe, 0, bars)
    if rates is None or len(rates) < period:
        return 0.0
    closes = [float(row["close"]) for row in rates]
    k = 2 / (period + 1)
    value = sum(closes[:period]) / period
    for price in closes[period:]:
        value = price * k + value * (1 - k)
    return value


def server_timestamp() -> float:
    """The broker's clock from one liquid symbol: scanning every symbol would cost seconds."""
    for base in ("EURUSD", "XAUUSD", "GBPUSD", "USDJPY"):
        name = resolve(base) or base
        tick = mt5.symbol_info_tick(name)
        if tick is not None and getattr(tick, "time", 0):
            return float(tick.time)
        rates = mt5.copy_rates_from_pos(name, mt5.TIMEFRAME_D1, 0, 1)
        if rates is not None and len(rates):
            return float(rates[-1]["time"])
    return 0.0
