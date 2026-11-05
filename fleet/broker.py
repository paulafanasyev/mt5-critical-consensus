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
    ticket_source: str = "unknown"


class BrokerError(RuntimeError):
    def __init__(self, message: str, retcode: int = 0) -> None:
        super().__init__(message)
        self.retcode = retcode


class StopsRefused(BrokerError):
    """The geometry we planned is one this broker's server will not accept — said before the send.

    Distinct from a rejection: a rejection costs a round trip and arrives as a retcode, this is the
    park reading the server's own contract first. The entry path journals it as a hold, not an error.
    """


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


def resolve_ticket(token: str, symbol_name: str, magic: int) -> tuple[int, str]:
    """Which position this fill made, and how sure of it we are.

    "token" is the answer we wrote into the order comment, so it names our lot. "only_position" is
    a netting account that added to a position whose comment is older than this order: one lot on
    this symbol and magic, so there is nothing else it can be. "unknown" is a hedge account with
    two lots on the same symbol, where guessing a ticket would join the wrong trade's outcome.
    """
    found = _find_by_token(token)
    if found is not None:
        return int(found.ticket), "token"
    same = open_positions(symbol_name, magic)
    if len(same) == 1:
        return int(same[0].ticket), "only_position"
    return 0, "unknown"


def _find_by_token(token: str) -> Any | None:
    """Look for the position this attempt would have created (token lives in the comment).

    The whole comment we sent, not the bare token: a token is a short string, and `12ab` is inside
    `fleet-12ab3c45` as surely as it is inside its own order. Matching the substring joined one
    fill to another lot's ticket, and the candidate book then paid that trade out of the wrong
    position's profit.
    """
    if not token:
        return None
    needle = f"fleet-{token}"
    for position in mt5.positions_get() or []:
        if needle in str(getattr(position, "comment", "")):
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


def stops_refusal(spec: BrokerSymbol, is_buy: bool, market_price: float, sl: float, tp: float,
                  current_sl: float = 0.0, current_tp: float = 0.0) -> str:
    """Why this broker's server would refuse the geometry we planned, or "" when it would take it.

    `trade_stops_level` and `trade_freeze_level` are facts about the server, not about the model:
    a stop or target closer to the live price than the first is retcode 10016, and a position whose
    standing stop or target sits inside the second is locked against any change. Learning either by
    sending the order means learning it while the money is already in flight, and for the modify
    path it means a rejected break-even that looks like a broker outage.

    Nothing here is clamped, on purpose. A lot is sized for the stop distance the strategy computed:
    pushing the stop out to the broker's minimum spends more than the reservation the portfolio book
    made, and moving a target changes what the candidate journal records as the trade's geometry. A
    level the server will not take is a refusal the operator can read, not a silent rewrite.
    """
    if spec.point <= 0:
        return ""
    gap = max(int(spec.stops_level), 0) * spec.point
    frozen = max(int(spec.freeze_level), 0) * spec.point
    if frozen > 0:
        for label, standing in (("стопа", current_sl), ("тейка", current_tp)):
            if standing > 0 and abs(market_price - standing) <= frozen:
                return (f"брокер блокирует правку: до существующего {label} "
                        f"{abs(market_price - standing) / spec.point:.1f} п. при freeze_level "
                        f"{int(spec.freeze_level)}")
    if gap <= 0:
        return ""
    # A buy's stop hangs below its price and its target above it; for a sell both roles swap. The
    # distance is measured on the side the level belongs to, so a level on the wrong side of the
    # price comes out negative and is refused by the same rule.
    for label, value, above in (("стоп", sl, -1.0), ("тейк", tp, 1.0)):
        if value <= 0:
            continue
        side = above if is_buy else -above
        distance = (value - market_price) if side > 0 else (market_price - value)
        if distance < gap:
            return (f"{label} в {distance / spec.point:.1f} п. от цены; брокер требует не ближе "
                    f"{int(spec.stops_level)} п. (trade_stops_level)")
    return ""


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
    refused = stops_refusal(spec, is_buy, price, sl, tp)
    if refused:
        raise StopsRefused(f"{spec.name}: {refused} — вход не отправлен")
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
                             position_ticket=int(found.ticket), ticket_source="token")
        raise
    filled_price = float(getattr(result, "price", 0.0) or price)
    filled_volume = float(getattr(result, "volume", 0.0) or volume)
    if filled_volume <= 0:
        filled_volume = volume
    # Slippage beyond the plan is real money: if it exceeded the allowed deviation, log it via
    # the retcode path; the caller compares price against the signal.
    # The answer names a deal and an order, not the position — in this API's OrderSendResult
    # (`retcode, deal, order, volume, price, bid, ask, comment, request, request_id`) there is no
    # position id at all, and a deal number used as a ticket joins to nothing. The token we wrote
    # in the order comment is the only name that reaches the position itself.
    ticket, source = resolve_ticket(token, spec.name, magic)
    return Execution(volume=filled_volume, price=filled_price,
                     retcode=int(result.retcode), token=token, position_ticket=ticket,
                     ticket_source=source)


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


def position_result(ticket: int) -> tuple[float, str] | None:
    """What the broker booked for a position that has left its book: gross P/L and last comment.

    None when the history says nothing about the ticket — a close the park did not order still has
    to be journalled, and `None` keeps it honest as an exit nobody priced. A closing deal carries
    the P/L and the entry deal carries none, which is the same rule `risk.daily_realized` uses.
    """
    deals = mt5.history_deals_get(position=int(ticket))
    if not deals:
        return None
    profit = sum(float(getattr(deal, "profit", 0.0) or 0.0)
                 for deal in deals if int(getattr(deal, "entry", -1)) == 1)
    return profit, str(getattr(deals[-1], "comment", "") or "").strip()


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


CLOCK_SYMBOLS = ("EURUSD", "XAUUSD", "GBPUSD", "USDJPY")


def server_timestamp() -> float:
    """The broker's clock: the newest tick among four liquid symbols, not the first one that answers.

    The first answer used to win. A symbol this terminal has not subscribed keeps a cached daily bar
    of the last time it was synced, so the day could come from history rather than from the quote:
    measured on the live park, the book said 2026-11-05 for the first cycles of a window and then
    2026-11-03 — the same terminal, four minutes later, because the symbol that answered first had
    changed. Four tick reads cost milliseconds; the daily-bar cache is consulted only when no tick
    answers at all, which is what a weekend looks like.
    """
    best = 0.0
    for base in CLOCK_SYMBOLS:
        name = resolve(base) or base
        tick = mt5.symbol_info_tick(name)
        if tick is not None and getattr(tick, "time", 0):
            best = max(best, float(tick.time))
    if best:
        return best
    for base in CLOCK_SYMBOLS:
        name = resolve(base) or base
        rates = mt5.copy_rates_from_pos(name, mt5.TIMEFRAME_D1, 0, 1)
        if rates is not None and len(rates):
            best = max(best, float(rates[-1]["time"]))
    return best
