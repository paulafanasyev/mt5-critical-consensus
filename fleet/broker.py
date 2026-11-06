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
    contract_size: float = 0.0
    profit_currency: str = ""
    tick_value_source: str = "tick"
    cash_divergence: float = 0.0
    calc_per_unit: float = 0.0
    cash_probe_error: str = ""
    tick_value_raw: float = 0.0

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


CASH_DIVERGENCE_TOLERANCE = 0.01
# How far the terminal's own calculator has to sit from a published field for that field to lose.
# Measured 2026-11-05 on login 114929502: USDJPY's `trade_tick_value` is priced at yesterday's yen and
# the calculator at today's — 0.20% apart on the same ruler. Anything inside that drift is the same
# number wearing a different coat; the metal fields were 900% apart.
CASH_CALC_TOLERANCE = 0.02


def account_currency() -> str:
    """The currency this terminal's account is kept in, or "" when the terminal will not say."""
    try:
        info = mt5.account_info()
    except Exception:  # noqa: BLE001 - a field read must never be the reason a cycle dies
        return ""
    return str(getattr(info, "currency", "") or "")


def _probe_volume(spec: BrokerSymbol) -> float:
    """A lot size this broker would accept for a calculator probe, or 0.0 when it would accept none."""
    if spec.volume_min <= 0:
        return 0.0
    step = spec.volume_step
    top = spec.volume_max if spec.volume_max > 0 else spec.volume_min
    # One lot is the cheapest probe; a contract whose minimum is above it (an index future at 5 lots)
    # has to be probed at its own minimum. Clipping at 1.0 instead made `_probe_volume` answer 0.0 for
    # such a symbol, the calculator was never asked, and an instrument whose two fields disagree was
    # then blocked from entries for ever by the very rule meant to settle them.
    volume = min(max(1.0, spec.volume_min), top)
    if step > 0:
        volume = math.floor(volume / step + 1e-9) * step
    if volume < spec.volume_min:
        return 0.0
    return round(volume, 12)


def calc_money_per_unit(spec: BrokerSymbol) -> tuple[float, str]:
    """What the terminal's own calculator charges for one lot over one price unit.

    `(value, "")` when it answers. `(0.0, why)` when it will not — and no why is ever a number the
    park sizes with, so the caller has to say what it did instead.

    Asked twice before it is called silent. A dropped answer is the expensive failure here: the ruler
    alternates between `proved by the calculator` and `never checked`, and every flip writes another
    row into the journal the operator reads. Measured 2026-11-05 on login 114929502 this terminal
    answered 200 of 200 probes in 0.009–0.121 ms, so the second call costs nothing where the first
    already answered, and buys the verdict where it did not. The reason reported is the last
    attempt's, in its own words.
    """
    volume = _probe_volume(spec)
    if volume <= 0:
        return 0.0, f"нет лота, который брокер принял бы для пробы (min {spec.volume_min})"
    why = "проба не сделана"
    for _attempt in range(2):
        money, why = _calc_once(spec, volume)
        if money > 0:
            return money, ""
    return 0.0, why


def _calc_once(spec: BrokerSymbol, volume: float) -> tuple[float, str]:
    """One read of the ticker and one call of the calculator, in the broker's own account currency."""
    try:
        tick = mt5.symbol_info_tick(spec.name)
    except Exception as exc:  # noqa: BLE001 - a probe that throws is a probe that did not answer
        return 0.0, f"тикер не отвечен: {exc}"
    if tick is None:
        return 0.0, "тикер не отвечен"
    price = float(getattr(tick, "bid", 0.0) or 0.0) or float(getattr(tick, "ask", 0.0) or 0.0)
    if price <= 0 or spec.tick_size <= 0:
        return 0.0, "цена для пробы нулевая"
    open_price = round_price(price, spec.digits)
    ticks = max(10, int(round(open_price * 0.002 / spec.tick_size)))
    close_price = round_price(open_price - ticks * spec.tick_size, spec.digits)
    distance = round(open_price - close_price, max(spec.digits, 8))
    if distance <= 0:
        return 0.0, "расстояние пробы схлопнулось в ноль на этих цифрах"
    try:
        raw = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, spec.name, volume, open_price, close_price)
    except Exception as exc:  # noqa: BLE001 - see above
        return 0.0, f"order_calc_profit бросил исключение: {exc}"
    if raw is None:
        return 0.0, "order_calc_profit вернул None"
    money = abs(float(raw))
    if money <= 0:
        return 0.0, "order_calc_profit говорит, что лот ничего не теряет"
    return money / (volume * distance), ""


def reconcile_tick_value(spec: BrokerSymbol, currency: str,
                         probe: callable | None = None) -> BrokerSymbol:
    """Let the broker's own calculator pick which of its two published fields is money.

    Measured 2026-11-05 on MetaQuotes-Demo (login 114929502, USD) — `order_calc_profit(BUY, sym,
    1.0 лот, p, p - d)` per price unit per lot against each field's prediction:

    | символ | tick-предсказание | контракт | расчёт брокера | кто побеждает |
    |--------|-------------------|----------|----------------|---------------|
    | XAUUSD | 10 | 100 | 100 | контракт (в 10 раз) |
    | XAGUSD | 500 | 5000 | 5000 | контракт (в 10 раз) |
    | EURUSD | 100000 | 100000 | 100000 | оба, тихо |
    | USDJPY | 633.24 | 100000 (в иенах) | 634.53 | тик, контракт — не те деньги |

    So neither field is trusted on its own and the calculator is not compared with a guess: it answers
    in the account's own money, which is why it also survives a cent account (`currency_scale` 0.01,
    where `trade_tick_value` is in cents and `trade_contract_size` is still in dollars — the two
    disagree by exactly 100 and the tick is the right one) and a broker that publishes a corrupt
    contract beside a correct tick. Getting this wrong does not lose a rounding error: sizing read the
    tick, so a gold lot carried ten times the risk percentage the registry promises.

    When the calculator will not answer and the two fields disagree, the instrument is marked
    `unproven` and the worker keeps its entries away from it — the park does not pick a ruler by
    preference on the way into someone's money. When the calculator will not answer and the fields
    agree (or there is no contract to compare with), the tick keeps sizing the lot under
    `tick-unchecked`: it is the ruler nobody was able to dispute, not a ruler the broker proved, and
    the journal has to be able to tell those two apart.
    """
    if spec.tick_size <= 0 or spec.tick_value <= 0:
        return spec
    tick_based = spec.tick_value / spec.tick_size
    contract_based = spec.contract_size if spec.contract_size > 0 else 0.0
    # Two fields are only in conflict when they claim the same currency: 100 000 of a yen-quoted pair
    # is not 100 000 dollars, and saying so is not a defect report.
    comparable = (contract_based > 0 and bool(currency)
                  and spec.profit_currency.upper() == currency.upper())
    divergence = (abs(contract_based - tick_based) / contract_based if comparable else 0.0)
    if contract_based <= 0:
        # An unpublished contract is not an opinion about the tick, and the calculator has nothing to
        # arbitrate, so it is never asked. The tick still sizes the lot — but under a name that says it
        # was never checked, so `risk_basis` cannot read like a verdict the broker gave.
        return dataclasses.replace(
            spec, tick_value_source="tick-unchecked",
            cash_probe_error="брокер не опубликовал контракт — калькулятор не о чём спрашивать")
    per, why = (probe or calc_money_per_unit)(spec)
    if per <= 0:
        if not (comparable and divergence > CASH_DIVERGENCE_TOLERANCE):
            # Nothing was in question, so the calculator's silence changes nothing — and it also
            # proves nothing. Same ruler, honest name.
            return dataclasses.replace(spec, tick_value_source="tick-unchecked",
                                       cash_probe_error=why,
                                       tick_value_raw=spec.tick_value)
        return dataclasses.replace(spec, tick_value_source="unproven",
                                   cash_divergence=round(divergence, 6),
                                   tick_value_raw=spec.tick_value,
                                   cash_probe_error=why)
    tick_error = abs(per - tick_based) / per
    contract_error = abs(per - contract_based) / per
    if tick_error <= CASH_CALC_TOLERANCE:
        # The tick is the ruler the broker's own money math repeats. "tick-proven" is said out loud
        # only where a published contract contradicted it; elsewhere nothing was in dispute.
        return dataclasses.replace(
            spec, tick_value_source=("tick-proven" if divergence > CASH_DIVERGENCE_TOLERANCE
                                     else "tick"),
            cash_divergence=round(divergence, 6), calc_per_unit=round(per, 8),
            tick_value_raw=spec.tick_value)
    if contract_error <= CASH_CALC_TOLERANCE:
        return dataclasses.replace(spec,
                                   tick_value=round(contract_based * spec.tick_size, 12),
                                   tick_value_source="contract",
                                   cash_divergence=round(divergence, 6),
                                   calc_per_unit=round(per, 8),
                                   tick_value_raw=spec.tick_value)
    return dataclasses.replace(spec, tick_value_source="unproven",
                               cash_divergence=round(divergence, 6),
                               calc_per_unit=round(per, 8),
                               tick_value_raw=spec.tick_value,
                               cash_probe_error=(f"расчёт брокера {per:.6g} не похож ни на тик "
                                                 f"({tick_based:.6g}, разрыв {tick_error:.1%}), ни на "
                                                 f"контракт ({contract_based:.6g}, разрыв "
                                                 f"{contract_error:.1%})"))


def symbol(base: str) -> BrokerSymbol | None:
    name = resolve(base)
    if name is None or not ensure_selected(name):
        return None
    info = mt5.symbol_info(name)
    tick = mt5.symbol_info_tick(name)
    if info is None or tick is None:
        return None
    spec = BrokerSymbol(
        name=name, digits=info.digits, point=info.point,
        tick_size=info.trade_tick_size or info.point,
        tick_value=info.trade_tick_value or 0.0,
        volume_min=info.volume_min, volume_max=info.volume_max,
        volume_step=info.volume_step or 0.01,
        stops_level=info.trade_stops_level, freeze_level=info.trade_freeze_level,
        spread=info.spread, filling=info.filling_mode,
        contract_size=float(getattr(info, "trade_contract_size", 0.0) or 0.0),
        profit_currency=str(getattr(info, "currency_profit", "") or ""))
    return reconcile_tick_value(spec, account_currency())


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


# Замер 2026-11-05 вечером на живом GTC (52183174, XAUUSD, `trade_stops_level = 150` = 1.50 цены):
# приказ со стопом в 1.60 ниже bid сервер отверг `invalid stops (10016)`, а `order_check` ровно той
# же геометрии в ту же минуту ответил «проверка пройдена». Разница — круг пути: уровень сервер меряет
# от bid того момента, когда приказ до него дошёл, а золото за секунды делает 0.10–0.30 цены. Стоп с
# запасом в 20 пункта сверх заявленного уровня этот же приказ принял (лот 363922616 открыт с 1.70).
STOP_MOVEMENT_POINTS = 20


def min_level_distance(spec: BrokerSymbol, spread: float) -> float:
    """The narrowest distance either level may hang from the price the fill pays, on this broker.

    Two costs the published `trade_stops_level` does not carry. The server judges a long's levels by
    the bid while the fill pays the ask, so the plan pays the spread before it starts measuring; and
    it judges them at the moment the order arrives, not the moment the plan was made, so a level
    parked exactly on the minimum is a level the next tick can cross — which is a rejection after
    the money is already in flight. The margin buys the round trip.
    """
    if spec.point <= 0:
        return 0.0
    level = (max(int(spec.stops_level), 0) + STOP_MOVEMENT_POINTS) * spec.point
    return level + max(float(spread), 0.0)


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

    `market_price` is the price the position would be closed at — the bid of a long, the ask of a
    short — not the price the entry is filled at. The two differ by the spread, and the server
    measures both levels of a long against the bid, so guarding with the ask hides a stop that has
    already crossed the market and bans a target the market would have taken.

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
    # A buy's stop hangs below its price and its target above it; for a sell both roles swap. The
    # distance is measured on the side the level belongs to, so a level on the wrong side of the
    # price comes out negative and is refused by the same rule — and that part holds with
    # `trade_stops_level` at zero, which is exactly what this broker names. Measured 2026-11-05 on
    # MetaQuotes-Demo (USDJPY, EURUSD, XAUUSD — all three at `trade_stops_level = 0`, asked with the
    # non-committing `order_check` and once with a real modify on our own 0.01 lot): a long's stop
    # one point above the live bid answers `invalid stops (10016)`, the same point below it answers
    # `done`, and a level sitting exactly at the bid is accepted. Equality is not a refusal, so the
    # test stays `distance < gap`, which at gap 0 refuses only the levels that crossed the price.
    for label, value, above in (("стоп", sl, -1.0), ("тейк", tp, 1.0)):
        if value <= 0:
            continue
        side = above if is_buy else -above
        distance = (value - market_price) if side > 0 else (market_price - value)
        if distance < 0:
            return (f"{label} по ту сторону цены на {abs(distance) / spec.point:.1f} п.: план "
                    f"пересекает рынок, и брокер такой геометрии не принимает")
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
    # The order is filled on the far side of the spread; the server judges its stops by the side the
    # position closes at. Measured 2026-11-05 at stops_level=0: a long's stop above the bid, however
    # far below the ask, is retcode 10016.
    checked_price = float(tick.bid if is_buy else tick.ask)
    refused = stops_refusal(spec, is_buy, checked_price, sl, tp)
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
    of the last time it was synced, so the day could come from history rather than from the quote.
    Measured on the live park 2026-11-05: three cycles of one terminal in a row answered
    2026-11-03, 2026-11-05, 2026-11-03, 35-70 seconds apart, and the same terminal gave seven
    regressions in twelve minutes — the day depended on which symbol answered first. Four tick reads
    cost milliseconds; the daily-bar cache is consulted only when no tick answers at all, which is
    what a weekend looks like.
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
