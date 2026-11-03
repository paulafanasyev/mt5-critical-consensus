"""Risk: per-account money rules, then the book.

Three things make this safe for real accounts: the day boundary follows the broker's clock rather
than the host clock, deposits and withdrawals are netted out of the daily result so a top-up can
never hide a loss, and every limit is expressed in money (USD) rather than in lots, because a lot
means a different amount of risk at every broker.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import math

import MetaTrader5 as mt5

from . import broker
from .config import Account, PortfolioLimits


@dataclasses.dataclass(slots=True)
class AccountSnapshot:
    login: int
    server: str
    currency: str
    balance: float
    equity: float
    margin_free: float
    leverage: int
    positions: int
    floating: float
    server_day: str = ""

    @classmethod
    def read(cls, server_day: str = "") -> "AccountSnapshot | None":
        info = mt5.account_info()
        if info is None:
            return None
        positions = mt5.positions_get() or []
        return cls(
            login=info.login, server=info.server, currency=info.currency,
            balance=info.balance, equity=info.equity, margin_free=info.margin_free,
            leverage=info.leverage, positions=len(positions),
            floating=sum(p.profit for p in positions),
            server_day=server_day or server_day_now())

    def rate_to_usd(self, scale: float = 1.0) -> float:
        return rate_to_usd(self.currency) * scale


def rate_to_usd(currency: str) -> float:
    """Convert an account currency into USD using the terminal's own quotes."""
    code = (currency or "USD").upper()
    if code in ("USD", "USDT", "USDC"):
        return 1.0
    direct = broker.resolve(f"{code}USD")
    if direct:
        tick = mt5.symbol_info_tick(direct)
        if tick is not None and tick.bid > 0:
            return float(tick.bid)
    inverse = broker.resolve(f"USD{code}")
    if inverse:
        tick = mt5.symbol_info_tick(inverse)
        if tick is not None and tick.bid > 0:
            return 1.0 / float(tick.bid)
    return 0.0  # unpriced: the account cannot be judged, so it may not open trades


def server_day_now() -> str:
    """The broker's trading day, derived from the newest tick, never from the host clock."""
    stamp = broker.server_timestamp()
    if stamp <= 0:
        return dt.datetime.now(dt.timezone.utc).date().isoformat()
    return dt.datetime.fromtimestamp(stamp, tz=dt.timezone.utc).date().isoformat()


@dataclasses.dataclass(slots=True)
class DayState:
    day: str
    start_balance: float
    net_flow: float = 0.0
    realized: float = 0.0
    stopped: bool = False

    @classmethod
    def fresh(cls, balance: float, day: str) -> "DayState":
        return cls(day=day, start_balance=balance)

    def rollover(self, balance: float, day: str) -> None:
        if day != self.day:
            self.day = day
            self.start_balance = balance
            self.net_flow = 0.0
            self.realized = 0.0
            self.stopped = False

    def daily_pct(self, equity: float) -> float:
        """Result for the day, net of deposits and withdrawals."""
        base = self.start_balance
        if base <= 0:
            return float("-inf")  # no capital: treat as stopped rather than as "no risk"
        return (equity - self.net_flow - base) / base * 100


def daily_net_flow(day_start: dt.datetime) -> float:
    """Deposits minus withdrawals booked by the broker since the day opened."""
    deals = mt5.history_deals_get(day_start, dt.datetime.now() + dt.timedelta(days=1)) or []
    total = 0.0
    for deal in deals:
        if int(getattr(deal, "type", -1)) == int(getattr(mt5, "DEAL_TYPE_BALANCE", 2)):
            total += float(getattr(deal, "profit", 0.0))
    return total


def day_start_datetime(day: str) -> dt.datetime:
    try:
        parsed = dt.date.fromisoformat(day)
    except ValueError:
        parsed = dt.datetime.now(dt.timezone.utc).date()
    return dt.datetime(parsed.year, parsed.month, parsed.day)


def positions_for(account: Account) -> list:
    """Every position on the account, not only ours: a manual lot still moves the equity."""
    return list(mt5.positions_get() or [])


def own_positions(account: Account) -> list:
    return [p for p in mt5.positions_get() or [] if p.magic == account.magic]


def can_open(account: Account, snapshot: AccountSnapshot, day: DayState,
             positions: list | None = None) -> tuple[bool, str]:
    risk = account.risk
    held = positions if positions is not None else positions_for(account)
    if day.stopped:
        return False, "дневной стоп счёта сработал"
    if day.daily_pct(snapshot.equity) <= -risk.daily_stop_pct:
        day.stopped = True
        return False, f"дневной стоп {risk.daily_stop_pct:.2f}% достигнут"
    if len(held) >= risk.max_open_positions:
        return False, "достигнут лимит позиций счёта"
    if snapshot.margin_free <= 0:
        return False, "нет свободной маржи"
    return True, ""


def position_risk_money(position, spec: broker.BrokerSymbol | None,
                        atr_fallback: float = 0.0, stop_atr: float = 1.6) -> float:
    """Money at risk if this position hits its own stop; unknown stop counts as the full risk."""
    if spec is None:
        return 0.0
    stop = float(getattr(position, "sl", 0.0) or 0.0)
    entry = float(position.price_open)
    if stop <= 0:
        return spec.distance_money(atr_fallback * stop_atr, float(position.volume))
    return spec.distance_money(abs(entry - stop), float(position.volume))


def book_risk_money(positions: list, specs: dict[str, broker.BrokerSymbol | None],
                    atr_by_symbol: dict[str, float] | None = None) -> float:
    atrs = atr_by_symbol or {}
    return sum(position_risk_money(p, specs.get(p.symbol), atrs.get(p.symbol, 0.0))
               for p in positions)


def signed_exposure(positions: list, predicate) -> float:
    total = 0.0
    for position in positions:
        if not predicate(position.symbol):
            continue
        sign = 1.0 if int(position.type) == int(mt5.POSITION_TYPE_BUY) else -1.0
        total += sign * float(position.volume)
    return total


def can_open_portfolio(limits: PortfolioLimits, equity_usd: float, risk_money: float,
                       new_risk_money: float, positions: list,
                       specs: dict[str, broker.BrokerSymbol | None],
                       day_pct: float, new_symbol: str = "",
                       new_volume: float = 0.0) -> tuple[bool, str]:
    """Book-wide gates, all in money and signed exposure, including the incoming order."""
    if day_pct <= -limits.daily_stop_pct:
        return False, f"дневной стоп портфеля {limits.daily_stop_pct:.2f}%"
    if len(positions) >= limits.max_open_positions:
        return False, "достигнут лимит позиций портфеля"
    if equity_usd > 0 and (risk_money + new_risk_money) > equity_usd * limits.max_total_risk_pct / 100:
        return False, "лимит суммарного риска портфеля"
    gold = abs(signed_exposure(positions, broker_is_gold))
    usd = abs(signed_exposure(positions, broker_is_usd_pair))
    if new_symbol and broker_is_gold(new_symbol):
        gold += abs(new_volume)
    elif new_symbol and broker_is_usd_pair(new_symbol):
        usd += abs(new_volume)
    if gold > limits.max_gold_lots:
        return False, "лимит по золоту"
    if usd > limits.max_usd_lots:
        return False, "лимит по долларовым парам"
    return True, ""


def broker_is_gold(symbol_name: str) -> bool:
    upper = symbol_name.upper()
    return "XAU" in upper or "GOLD" in upper


def broker_is_usd_pair(symbol_name: str) -> bool:
    upper = symbol_name.upper()
    return (upper.startswith("USD") or upper.endswith("USD")) and not broker_is_gold(upper)


def size_for(account: Account, spec: broker.BrokerSymbol, stop_distance: float,
             balance: float, risk_cap_pct: float | None = None) -> float:
    """Lots whose loss at the stop is within the risk budget, rounded DOWN to the broker's step."""
    if stop_distance <= 0 or spec.tick_size <= 0 or spec.tick_value <= 0:
        return 0.0
    pct = account.risk.risk_per_trade_pct if risk_cap_pct is None else risk_cap_pct
    risk_money = balance * pct / 100
    if risk_money <= 0:
        return 0.0
    loss_per_lot = (stop_distance / spec.tick_size) * spec.tick_value
    if loss_per_lot <= 0:
        return 0.0
    volume = broker.normalize_volume(risk_money / loss_per_lot, spec, account.risk.max_lots)
    if volume < spec.volume_min:
        return 0.0
    if volume < account.risk.min_lots:
        return 0.0
    if spec.distance_money(stop_distance, volume) > risk_money:
        return 0.0  # the minimum step still risks more than allowed: skip rather than oversize
    return volume


def validate_risk_profile(account: Account) -> list[str]:
    """Catch configurations whose arithmetic cannot hold: the day stops before entries do."""
    problems: list[str] = []
    risk = account.risk
    if risk.risk_per_trade_pct <= 0:
        problems.append(f"{account.name}: risk_per_trade_pct должен быть > 0")
    if risk.max_open_positions * risk.risk_per_trade_pct > risk.daily_stop_pct:
        problems.append(
            f"{account.name}: {risk.max_open_positions} позиций по "
            f"{risk.risk_per_trade_pct}% не помещаются в дневной стоп {risk.daily_stop_pct}%")
    if risk.min_lots > risk.max_lots:
        problems.append(f"{account.name}: min_lots больше max_lots")
    return problems


def daily_summary(snapshots: list[AccountSnapshot]) -> dict[str, float]:
    """Book totals in USD; accounts whose currency has no quote are reported, not guessed."""
    balance = equity = floating = 0.0
    unpriced = 0
    for snapshot in snapshots:
        rate = snapshot.rate_to_usd()
        if rate <= 0:
            unpriced += 1
            continue
        balance += snapshot.balance * rate
        equity += snapshot.equity * rate
        floating += snapshot.floating * rate
    return {"balance": balance, "equity": equity, "floating": floating,
            "accounts": float(len(snapshots)), "unpriced_accounts": float(unpriced)}


def floor_lots(volume: float, step: float) -> float:
    if step <= 0:
        return 0.0
    return math.floor(volume / step + 1e-9) * step
