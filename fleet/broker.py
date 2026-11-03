"""Broker adapter: turn a bare symbol name into whatever that broker actually calls it.

Every MT5 broker renames instruments (``EURUSD``, ``EURUSDm``, ``EURUSD.a``, ``XAUUSD#``) and
sets its own lot step, stops level and filling mode. The fleet writes strategies against plain
names; this module resolves them per account and sends orders that survive requotes.
"""
from __future__ import annotations

import dataclasses
import time
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

    @property
    def max_spread_points(self) -> int:
        return max(10, int(self.stops_level * 2))


class BrokerError(RuntimeError):
    pass


def describe(code: int) -> str:
    """Readable name for a trade server return code."""
    names = {
        mt5.TRADE_RETCODE_REQUOTE: "requote",
        mt5.TRADE_RETCODE_REJECT: "reject",
        mt5.TRADE_RETCODE_INVALID: "invalid request",
        mt5.TRADE_RETCODE_INVALID_VOLUME: "invalid volume",
        mt5.TRADE_RETCODE_INVALID_PRICE: "invalid price",
        mt5.TRADE_RETCODE_INVALID_STOPS: "invalid stops",
        mt5.TRADE_RETCODE_TRADE_DISABLED: "trading disabled",
        mt5.TRADE_RETCODE_MARKET_CLOSED: "market closed",
        mt5.TRADE_RETCODE_NO_MONEY: "no money",
        mt5.TRADE_RETCODE_PRICE_CHANGED: "price changed",
        mt5.TRADE_RETCODE_PRICE_OFF: "no quotes",
        mt5.TRADE_RETCODE_INVALID_EXPIRATION: "invalid expiration",
        mt5.TRADE_RETCODE_ORDER_CHANGED: "order changed",
        mt5.TRADE_RETCODE_TOO_MANY_REQUESTS: "too many requests",
        mt5.TRADE_RETCODE_NO_CHANGES: "no changes",
        mt5.TRADE_RETCODE_SERVER_DISABLES_AT: "autotrading disabled by server",
        mt5.TRADE_RETCODE_CLIENT_DISABLES_AT: "autotrading disabled by client",
        mt5.TRADE_RETCODE_LOCKED: "request locked",
        mt5.TRADE_RETCODE_FROZEN: "order frozen",
        mt5.TRADE_RETCODE_CONNECTION: "no connection",
        mt5.TRADE_RETCODE_LIMIT_ORDERS: "order limit reached",
        mt5.TRADE_RETCODE_LIMIT_VOLUME: "volume limit reached",
        mt5.TRADE_RETCODE_INVALID_FILL: "invalid filling mode",
        mt5.TRADE_RETCODE_ONLY_REAL: "only real accounts",
        mt5.TRADE_RETCODE_TIMEOUT: "timeout",
    }
    return names.get(code, f"code {code}")


def resolve(base: str) -> str | None:
    """Find the broker's own name for a plain symbol: exact first, then the shortest variant."""
    if not base:
        return None
    exact = mt5.symbol_info(base)
    if exact is not None:
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

    def score(name: str) -> tuple[int, int, str]:
        suffix = name[len(base):]
        # prefer the variant with the fewest decorations: EURUSDm beats EURUSD.micro
        return (len(suffix), 0 if suffix.isalpha() else 1, name)

    return sorted(candidates, key=score)[0]


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
                break
    return bool(info and info.visible)


def symbol(base: str) -> BrokerSymbol | None:
    name = resolve(base)
    if name is None or not ensure_selected(name):
        return None
    info = mt5.symbol_info(name)
    tick = mt5.symbol_info_tick(name)
    if info is None or tick is None:
        return None
    return BrokerSymbol(
        name=name,
        digits=info.digits,
        point=info.point,
        tick_size=info.trade_tick_size or info.point,
        tick_value=info.trade_tick_value or 0.0,
        volume_min=info.volume_min,
        volume_max=info.volume_max,
        volume_step=info.volume_step,
        stops_level=info.trade_stops_level,
        freeze_level=info.trade_freeze_level,
        spread=info.spread,
        filling=info.filling_mode,
    )


def round_price(value: float, digits: int) -> float:
    return round(value, digits)


def normalize_volume(volume: float, spec: BrokerSymbol, lot_cap: float) -> float:
    """Clamp the size to the broker's step and bounds; returns 0.0 when nothing fits."""
    cap = min(spec.volume_max, lot_cap)
    if cap < spec.volume_min:
        return 0.0
    stepped = round(volume / spec.volume_step) * spec.volume_step
    stepped = min(cap, max(spec.volume_min, stepped))
    decimals = max(0, len(f"{spec.volume_step:.10f}".rstrip("0").split(".")[1])) if spec.volume_step else 2
    return round(stepped, decimals)


def filling_mode(spec: BrokerSymbol) -> int:
    """Pick a filling mode the instrument actually allows."""
    allowed = spec.filling
    if allowed & 1:  # FOK
        return mt5.ORDER_FILLING_FOK
    if allowed & 2:  # IOC
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def spread_ok(spec: BrokerSymbol, max_points: int) -> bool:
    tick = mt5.symbol_info_tick(spec.name)
    if tick is None:
        return False
    points = (tick.ask - tick.bid) / spec.point if spec.point else 0
    return points <= max_points


def open_positions(symbol_name: str | None = None, magic: int | None = None) -> list[Any]:
    positions = mt5.positions_get(symbol=symbol_name) if symbol_name else mt5.positions_get()
    if positions is None:
        return []
    if magic is None:
        return list(positions)
    return [p for p in positions if p.magic == magic]


def _send(request: dict[str, Any], attempts: int = 3) -> Any:
    last = None
    for attempt in range(attempts):
        result = mt5.order_send(request)
        if result is None:
            last = f"order_send вернул None (код {mt5.last_error()})"
            time.sleep(0.7)
            continue
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            return result
        last = f"{describe(result.retcode)} (retcode {result.retcode})"
        if result.retcode in (mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_PRICE_CHANGED,
                              mt5.TRADE_RETCODE_PRICE_OFF, mt5.TRADE_RETCODE_TOO_MANY_REQUESTS):
            time.sleep(0.8 + attempt * 0.7)
            continue
        break
    raise BrokerError(f"сделка не прошла: {last}")


def market_order(account_magic: int, spec: BrokerSymbol, side: str, volume: float,
                 sl: float, tp: float, comment: str = "fleet") -> Any:
    tick = mt5.symbol_info_tick(spec.name)
    if tick is None:
        raise BrokerError(f"нет тика по {spec.name}")
    is_buy = side.lower() == "buy"
    price = tick.ask if is_buy else tick.bid
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": spec.name,
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price": round_price(price, spec.digits),
        "sl": round_price(sl, spec.digits),
        "tp": round_price(tp, spec.digits),
        "deviation": max(10, int(spec.point * 2 * 1e4)),
        "magic": account_magic,
        "comment": comment[:31],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_mode(spec),
    }
    return _send(request)


def modify_position(ticket: int, symbol_name: str, sl: float, tp: float, digits: int) -> Any:
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "symbol": symbol_name,
        "sl": round_price(sl, digits),
        "tp": round_price(tp, digits),
    }
    return _send(request, attempts=2)


def close_position(position: Any, spec: BrokerSymbol, comment: str = "fleet-exit") -> Any:
    tick = mt5.symbol_info_tick(spec.name)
    if tick is None:
        raise BrokerError(f"нет тика по {spec.name}")
    is_buy = position.type == mt5.POSITION_TYPE_BUY
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": spec.name,
        "volume": position.volume,
        "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
        "position": position.ticket,
        "price": round_price(tick.bid if is_buy else tick.ask, spec.digits),
        "deviation": max(10, int(spec.point * 2 * 1e4)),
        "magic": position.magic,
        "comment": comment[:31],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_mode(spec),
    }
    return _send(request)


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
