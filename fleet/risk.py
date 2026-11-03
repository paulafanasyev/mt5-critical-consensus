"""Risk: first per account, then across the whole book.

Every account carries its own daily stop so one broken broker cannot bleed the fleet, and the
portfolio layer caps aggregate exposure by asset class so a hundred accounts all buying gold do
not become one giant gold position.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

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

    @classmethod
    def read(cls) -> "AccountSnapshot | None":
        info = mt5.account_info()
        if info is None:
            return None
        positions = mt5.positions_get() or []
        return cls(
            login=info.login,
            server=info.server,
            currency=info.currency,
            balance=info.balance,
            equity=info.equity,
            margin_free=info.margin_free,
            leverage=info.leverage,
            positions=len(positions),
            floating=sum(p.profit for p in positions),
        )


@dataclasses.dataclass(slots=True)
class DayState:
    """What needs remembering about one account across a trading day."""

    day: dt.date
    start_balance: float
    realized: float = 0.0
    stopped: bool = False

    @classmethod
    def fresh(cls, balance: float) -> "DayState":
        return cls(day=dt.date.today(), start_balance=balance)

    def rollover(self, balance: float) -> None:
        today = dt.date.today()
        if today != self.day:
            self.day = today
            self.start_balance = balance
            self.realized = 0.0
            self.stopped = False

    def daily_pct(self, equity: float) -> float:
        if self.start_balance <= 0:
            return 0.0
        return (equity - self.start_balance) / self.start_balance * 100


def positions_for(account: Account) -> list:
    positions = mt5.positions_get() or []
    return [p for p in positions if p.magic == account.magic]


def can_open(account: Account, snapshot: AccountSnapshot, day: DayState) -> tuple[bool, str]:
    """Gate a new entry for a single account."""
    risk = account.risk
    if day.stopped:
        return False, "дневной стоп счёта сработал"
    if day.daily_pct(snapshot.equity) <= -risk.daily_stop_pct:
        day.stopped = True
        return False, f"дневной стоп {risk.daily_stop_pct:.2f}% достигнут"
    if len(positions_for(account)) >= risk.max_open_positions:
        return False, "достигнут лимит позиций счёта"
    if snapshot.margin_free <= 0:
        return False, "нет свободной маржи"
    return True, ""


def can_open_portfolio(limits: PortfolioLimits, snapshots: list[AccountSnapshot],
                       positions: list, day_pct: float) -> tuple[bool, str]:
    """Gate a new entry across every enabled account."""
    if day_pct <= -limits.daily_stop_pct:
        return False, f"дневной стоп портфеля {limits.daily_stop_pct:.2f}%"
    if len(positions) >= limits.max_open_positions:
        return False, "достигнут лимит позиций портфеля"
    gold = sum(p.volume for p in positions if "XAU" in p.symbol or "GOLD" in p.symbol.upper())
    if gold >= limits.max_gold_lots:
        return False, "лимит по золоту"
    usd = sum(p.volume for p in positions
              if p.symbol.upper().endswith("USD") and "XAU" not in p.symbol.upper())
    if usd >= limits.max_usd_lots:
        return False, "лимит по долларовым парам"
    return True, ""


def size_for(account: Account, spec: broker.BrokerSymbol, stop_distance: float,
             balance: float) -> float:
    """Lots that lose roughly risk_per_trade_pct of balance when the stop is hit."""
    if stop_distance <= 0 or spec.tick_size <= 0 or spec.tick_value <= 0:
        return 0.0
    risk_money = balance * account.risk.risk_per_trade_pct / 100
    ticks = stop_distance / spec.tick_size
    loss_per_lot = ticks * spec.tick_value
    if loss_per_lot <= 0:
        return 0.0
    raw = risk_money / loss_per_lot
    volume = broker.normalize_volume(raw, spec, account.risk.max_lots)
    return max(0.0, volume)


def daily_summary(snapshots: list[AccountSnapshot]) -> dict[str, float]:
    balance = sum(s.balance for s in snapshots)
    equity = sum(s.equity for s in snapshots)
    floating = sum(s.floating for s in snapshots)
    return {"balance": balance, "equity": equity, "floating": floating,
            "accounts": float(len(snapshots))}
