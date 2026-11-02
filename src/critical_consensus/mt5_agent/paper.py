"""A simulated MT5 account driven bar-by-bar from real historical candles.

The broker never sees these orders: fills, stops and equity are computed locally so
the agent can be exercised without a terminal, a broker account or real money.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from ..market_data import MarketBar, validate_bar_series
from . import codes
from .backend import TIMEFRAME_DURATION, AccountInfo, PositionSnapshot, SymbolSpec, Tick

UTC = timezone.utc


def _bar_close(bar: MarketBar) -> datetime:
    return bar.time + TIMEFRAME_DURATION[bar.timeframe]


@dataclass(frozen=True)
class PaperTrade:
    ticket: int
    symbol: str
    direction: str
    volume: float
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    opened_at: datetime
    closed_at: datetime
    pnl: float
    exit_reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"ticket": self.ticket, "symbol": self.symbol, "direction": self.direction,
                "volume": self.volume, "entry_price": self.entry_price,
                "exit_price": self.exit_price, "stop_loss": self.stop_loss,
                "take_profit": self.take_profit, "opened_at": self.opened_at.isoformat(),
                "closed_at": self.closed_at.isoformat(), "pnl": self.pnl,
                "exit_reason": self.exit_reason}


def _pnl(spec: SymbolSpec, direction: str, entry: float, exit_price: float,
         volume: float) -> float:
    sign = 1.0 if direction == "BUY" else -1.0
    return (exit_price - entry) * spec.cash_per_price_unit * volume * sign


class PaperBroker:
    """Read side implements ``MT5Backend``; write side is paper-only bookkeeping."""

    def __init__(self, *, bars: Mapping[str, Sequence[MarketBar]], spec: SymbolSpec,
                 login: int = 1_000_000_000, server: str = "PAPER-LOCAL",
                 currency: str = "USD", starting_balance: float = 10_000.0,
                 leverage: int = 100, server_utc_offset_seconds: int = 0) -> None:
        if starting_balance <= 0:
            raise ValueError("starting_balance must be positive")
        self._spec = spec
        self._rows: dict[str, list[MarketBar]] = {}
        for symbol, series in bars.items():
            self._rows[str(symbol).strip().upper()] = validate_bar_series(
                series, symbol=symbol, timeframe=series[0].timeframe if series else None)
        if not self._rows:
            raise ValueError("at least one bar series is required")
        if spec.symbol.strip().upper() not in self._rows:
            raise ValueError("the broker spec symbol must have a bar series")
        self._login = int(login)
        self._server = str(server).strip()
        self._currency = str(currency).strip()
        self._balance = float(starting_balance)
        self._leverage = int(leverage)
        self._offset = int(server_utc_offset_seconds)
        self._cursor: dict[str, int] = {symbol: -1 for symbol in self._rows}
        self._processed: dict[str, int] = {symbol: -1 for symbol in self._rows}
        self._open: list[PositionSnapshot] = []
        self._floating: dict[int, float] = {}
        self._trades: list[PaperTrade] = []
        self._next_ticket = 1
        self._peak_equity = float(starting_balance)
        self._day_start_equity = float(starting_balance)
        self._day = None

    # ------------------------------------------------------------------ clock
    def seek(self, moment: datetime) -> None:
        """Move the visible history to ``moment``; bars opening after it stay unreadable.

        The last visible bar is treated as closed, so ``now()`` is its close time. The runner
        seeks at each bar's open stamp, which makes the replay process one closed bar at a
        time; an arbitrary timestamp inside a bar would expose that bar's close early.
        """
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("seek moment must be timezone-aware")
        target = moment.astimezone(UTC)
        for symbol, series in self._rows.items():
            index = -1
            for position, bar in enumerate(series):
                if bar.time <= target:
                    index = position
                else:
                    break
            self._cursor[symbol] = index
        self._process_exits()
        # The day baseline must include every exit that just filled, otherwise a seek that
        # crosses midnight books the loss on the previous day and enlarges today's budget.
        self._roll_day(self._visible_clock(target))

    def _visible_clock(self, fallback: datetime) -> datetime:
        index = self._cursor.get(self._spec.symbol, -1)
        if index < 0:
            return fallback
        return _bar_close(self._rows[self._spec.symbol][index])

    def now(self) -> datetime:
        symbol = self._spec.symbol
        index = self._cursor.get(symbol, -1)
        if index < 0:
            raise RuntimeError("paper broker has not been seeked to a visible bar")
        return _bar_close(self._rows[symbol][index])

    def _roll_day(self, moment: datetime) -> None:
        day = (moment + timedelta(seconds=self._offset)).date()
        if self._day != day:
            self._day = day
            self._day_start_equity = self.equity()

    # ------------------------------------------------------- read-only surface
    def symbol_spec(self, symbol: str) -> SymbolSpec:
        if symbol.strip().upper() != self._spec.symbol:
            raise ValueError(f"paper broker has no specification for {symbol.upper()}")
        return self._spec

    def _spread_price(self, bar: MarketBar) -> float:
        """MT5 bars are built from bid prices, so the ask of a bar is its bid plus the spread."""
        # ``spread_points`` is optional in a bar (a CSV without a Spread column yields None),
        # so an absent or non-positive value falls back to the symbol's declared spread.
        points = (self._spec.spread_points
                  if bar.spread_points is None or bar.spread_points <= 0
                  else bar.spread_points)
        return points * self._spec.point

    def tick(self, symbol: str) -> Tick:
        name = symbol.strip().upper()
        index = self._cursor.get(name, -1)
        if index is None or index < 0:
            raise RuntimeError(f"no paper quote available for {name} yet")
        bar = self._rows[name][index]
        bid = bar.close
        return Tick(symbol=name, bid=bid, ask=bid + self._spread_price(bar),
                    server_time=_bar_close(bar), server_utc_offset_seconds=self._offset)

    def bars(self, symbol: str, timeframe: str, count: int) -> list[MarketBar]:
        name = symbol.strip().upper()
        series = self._rows.get(name)
        if series is None:
            raise ValueError(f"paper broker has no bars for {name}")
        index = self._cursor.get(name, -1)
        if index < 0:
            return []
        window = series[max(0, index - count + 1):index + 1]
        return validate_bar_series(window, symbol=name, timeframe=timeframe.strip().upper())

    def positions(self) -> list[PositionSnapshot]:
        return list(self._open)

    def margin_used(self) -> float:
        return sum(self._spec.margin_requirement(position.volume, self._leverage,
                                                 position.entry_price)
                   for position in self._open)

    def margin_free(self) -> float:
        return self.equity() - self.margin_used()

    def account(self) -> AccountInfo:
        return AccountInfo(login=self._login, server=self._server, currency=self._currency,
                           balance=self._balance, equity=self.equity(),
                           margin_free=self.margin_free(), leverage=self._leverage,
                           trade_mode="demo")

    def shutdown(self) -> None:
        return None

    # ------------------------------------------------------------ paper state
    def equity(self) -> float:
        return self._balance + sum(self._floating.values())

    @property
    def day_start_equity(self) -> float:
        return self._day_start_equity

    @property
    def peak_equity(self) -> float:
        return self._peak_equity

    @property
    def server_utc_offset_seconds(self) -> int:
        return self._offset

    @property
    def realized_daily_pnl(self) -> float:
        """P&L already banked on the current broker-server day.

        Equity alone does not lock a loss in: close a loser, the market rebounds, and the
        floating cap says the day still has budget. This is the number the daily lockout needs.
        Before the first seek there is no server day yet, and nothing has been banked.
        """
        if self._day is None:
            return 0.0
        return sum(trade.pnl for trade in self._trades
                   if (trade.closed_at + timedelta(seconds=self._offset)).date() == self._day)

    @property
    def trades(self) -> list[PaperTrade]:
        return list(self._trades)

    @property
    def realized_pnl(self) -> float:
        return sum(trade.pnl for trade in self._trades)

    def open_position(self, *, direction: str, volume: float, entry_price: float,
                      stop_loss: float, take_profit: float,
                      opened_at: datetime | None = None) -> PositionSnapshot:
        direction = direction.strip().upper()
        if direction not in {"BUY", "SELL"}:
            raise ValueError("direction must be BUY or SELL")
        if volume < self._spec.volume_min - 1e-12 or volume <= 0:
            raise ValueError("volume is below the broker minimum lot")
        if volume > self._spec.volume_max + 1e-12:
            raise ValueError("volume exceeds the broker maximum lot")
        steps = volume / self._spec.volume_step
        if abs(steps - round(steps)) > 1e-9:
            raise ValueError("volume is not a multiple of the broker step")
        if not math.isfinite(entry_price) or entry_price <= 0:
            raise ValueError("entry price must be positive and finite")
        for name, value in (("stop loss", stop_loss), ("take profit", take_profit)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if direction == "BUY" and not stop_loss < entry_price < take_profit:
            raise ValueError("BUY geometry requires stop < entry < target")
        if direction == "SELL" and not take_profit < entry_price < stop_loss:
            raise ValueError("SELL geometry requires target < entry < stop")
        # A real broker refuses a lot it cannot fund; the paper book must refuse it too,
        # otherwise a tiny stop silently sizes into an untradeable position.
        if self._spec.margin_requirement(volume, self._leverage, entry_price) \
                > self.margin_free():
            raise ValueError("volume requires more margin than the account has free")
        moment = opened_at or self.now()
        position = PositionSnapshot(ticket=self._next_ticket, symbol=self._spec.symbol,
                                    direction=direction, volume=volume, entry_price=entry_price,
                                    stop_loss=stop_loss, take_profit=take_profit,
                                    open_time=moment)
        self._next_ticket += 1
        self._open.append(position)
        self._floating[position.ticket] = 0.0
        return position

    def close_all(self, reason: str = codes.MANUAL) -> list[PaperTrade]:
        closed = []
        for position in list(self._open):
            tick = self.tick(position.symbol)
            price = tick.bid if position.direction == "BUY" else tick.ask
            closed.append(self._close(position, price, reason, self.now()))
        self._refresh_floating()
        return closed

    def _process_exits(self) -> None:
        """Walk every bar since the last evaluation so a jumped-over stop is not lost.

        A BUY is closed at the bid and a SELL at the ask, and MT5 bars are bid prices, so a
        SELL level is compared against ``bar + spread``. When a bar opens beyond a level the
        fill is the open price, not the level: that is a gap, and pretending it filled at the
        stop would book a smaller loss than the broker would have taken.
        """
        for symbol, series in self._rows.items():
            cursor = self._cursor.get(symbol, -1)
            last = self._processed.get(symbol, -1)
            for index in range(last + 1, cursor + 1):
                bar = series[index]
                spread = self._spread_price(bar)
                for position in [item for item in self._open if item.symbol == symbol]:
                    if position.open_time > bar.time:
                        continue
                    stop, target = position.stop_loss, position.take_profit
                    if position.direction == "BUY":
                        open_price = bar.open
                        # One bar can touch both levels; the stop is always evaluated first.
                        if bar.low <= stop:
                            gap = open_price <= stop
                            self._close(position, min(open_price, stop),
                                        codes.STOP_LOSS_GAP if gap else codes.STOP_LOSS, bar.time)
                        elif bar.high >= target:
                            gap = open_price >= target
                            self._close(position, max(open_price, target),
                                        codes.TAKE_PROFIT_GAP if gap else codes.TAKE_PROFIT, bar.time)
                    else:
                        open_price = bar.open + spread
                        if bar.high + spread >= stop:
                            gap = open_price >= stop
                            self._close(position, max(open_price, stop),
                                        codes.STOP_LOSS_GAP if gap else codes.STOP_LOSS, bar.time)
                        elif bar.low + spread <= target:
                            gap = open_price <= target
                            self._close(position, min(open_price, target),
                                        codes.TAKE_PROFIT_GAP if gap else codes.TAKE_PROFIT, bar.time)
            self._processed[symbol] = cursor
        self._refresh_floating()

    def _close(self, position: PositionSnapshot, price: float, reason: str,
               at: datetime) -> PaperTrade:
        spec = self.symbol_spec(position.symbol)
        pnl = _pnl(spec, position.direction, position.entry_price, price, position.volume)
        trade = PaperTrade(ticket=position.ticket, symbol=position.symbol,
                           direction=position.direction, volume=position.volume,
                           entry_price=position.entry_price, exit_price=price,
                           stop_loss=position.stop_loss, take_profit=position.take_profit,
                           opened_at=position.open_time, closed_at=at, pnl=pnl,
                           exit_reason=reason)
        self._open.remove(position)
        self._floating.pop(position.ticket, None)
        self._balance += pnl
        self._trades.append(trade)
        return trade

    def _refresh_floating(self) -> None:
        floating: dict[int, float] = {}
        for position in self._open:
            try:
                tick = self.tick(position.symbol)
            except RuntimeError:
                floating[position.ticket] = self._floating.get(position.ticket, 0.0)
                continue
            price = tick.bid if position.direction == "BUY" else tick.ask
            floating[position.ticket] = _pnl(self._spec, position.direction,
                                             position.entry_price, price, position.volume)
        self._floating = floating
        self._peak_equity = max(self._peak_equity, self.equity())
