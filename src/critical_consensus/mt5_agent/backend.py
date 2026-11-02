"""Backend abstraction over a MetaTrader 5 terminal.

Only order-free terminal calls appear here (account info, symbol specification,
ticks, rates, open positions). ``symbol_spec()`` may call ``symbol_select`` once to
add a missing symbol to Market Watch — the only call that mutates terminal state.
There is deliberately no order placement, modification or cancellation surface.
"""
from __future__ import annotations

import importlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Protocol, Sequence

from ..market_data import (MarketBar, SUPPORTED_TIMEFRAMES, normalize_rate_rows, server_offset,
                           validate_bar_series)

UTC = timezone.utc
_TIMEFRAME_CONSTANTS = {
    "M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30", "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1", "W1": "TIMEFRAME_W1", "MN1": "TIMEFRAME_MN1",
}
# A bar is stamped at its open, so it closes at ``time + TIMEFRAME_DURATION[timeframe]``.
# The paper broker and the strategy's freshness check must agree on that period.
TIMEFRAME_DURATION = {
    "M1": timedelta(minutes=1), "M5": timedelta(minutes=5), "M15": timedelta(minutes=15),
    "M30": timedelta(minutes=30), "H1": timedelta(hours=1), "H4": timedelta(hours=4),
    "D1": timedelta(days=1), "W1": timedelta(weeks=1), "MN1": timedelta(days=30),
}
_TRADE_MODE_NAMES = {0: "demo", 1: "contest", 2: "real"}
# A terminal stamp may run slightly ahead of this machine's clock; anything beyond this is a
# mis-set server_utc_offset_seconds, and letting it through would make every quote "from the future".
_MAX_FUTURE_SKEW = timedelta(seconds=120)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or isinstance(value, str):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any) -> float:
    number = _finite(value)
    if number is None or number <= 0:
        raise ValueError("value must be a positive finite number")
    return number


@dataclass(frozen=True)
class AccountInfo:
    login: int
    server: str
    currency: str
    balance: float
    equity: float
    margin_free: float
    leverage: int
    trade_mode: str

    def __post_init__(self) -> None:
        if isinstance(self.login, bool) or self.login <= 0:
            raise ValueError("account login must be a positive integer")
        if not self.server.strip():
            raise ValueError("account server must be non-empty")
        if not self.currency.strip():
            raise ValueError("account currency must be non-empty")
        for name in ("balance", "equity", "margin_free"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value):
                raise ValueError(f"account {name} must be a finite number")
        if self.balance <= 0 or self.equity <= 0:
            raise ValueError("account balance and equity must be positive")
        if isinstance(self.leverage, bool) or self.leverage <= 0:
            raise ValueError("leverage must be a positive integer")
        if self.trade_mode not in _TRADE_MODE_NAMES.values():
            raise ValueError("unknown account trade mode")

    @classmethod
    def from_terminal(cls, info: Any) -> "AccountInfo":
        """Map a terminal ``AccountInfoStruct`` with no defaults and no silent coercion.

        Anything the terminal does not answer with a real value raises here, so a broken
        adapter surfaces as ACCOUNT_READ_FAILED instead of a fabricated zero.
        """
        login = getattr(info, "login", None)
        if isinstance(login, bool) or not isinstance(login, int) or login <= 0:
            raise RuntimeError("MT5 account login is missing or not an integer")
        server = getattr(info, "server", None)
        currency = getattr(info, "currency", None)
        if not isinstance(server, str) or not isinstance(currency, str):
            raise RuntimeError("MT5 account server and currency must be strings")
        mode_code = getattr(info, "trade_mode", None)
        if isinstance(mode_code, bool) or not isinstance(mode_code, int) \
                or mode_code not in _TRADE_MODE_NAMES:
            raise RuntimeError("MT5 returned an unknown account trade mode")
        # Free margin may legitimately be negative during a margin call; only non-numeric
        # or non-finite values are unusable.
        numbers = {name: _finite(getattr(info, name, None))
                   for name in ("balance", "equity", "margin_free", "leverage")}
        if any(numbers[name] is None for name in ("balance", "equity", "margin_free")):
            raise RuntimeError("MT5 account returned a missing or non-numeric money field")
        return cls(login=login, server=server, currency=currency, balance=numbers["balance"],
                   equity=numbers["equity"], margin_free=numbers["margin_free"],
                   leverage=int(numbers["leverage"] or 0),
                   trade_mode=_TRADE_MODE_NAMES[mode_code])

    @property
    def is_demo(self) -> bool:
        return self.trade_mode == "demo"

    def as_dict(self) -> dict[str, Any]:
        return {"login": self.login, "server": self.server, "currency": self.currency,
                "balance": self.balance, "equity": self.equity, "margin_free": self.margin_free,
                "leverage": self.leverage, "trade_mode": self.trade_mode}


@dataclass(frozen=True)
class SymbolSpec:
    symbol: str
    point: float
    digits: int
    tick_size: float
    tick_value: float
    volume_min: float
    volume_max: float
    volume_step: float
    spread_points: int
    stops_level_points: int
    # Units of the base contract behind one lot: 100 000 EUR, 100 troy ounces, 5 000 silver
    # ounces. Optional because a policy-written spec may predate it; a terminal always has it.
    contract_size: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        for name in ("point", "tick_size", "tick_value", "volume_min", "volume_max", "volume_step"):
            object.__setattr__(self, name, _positive(getattr(self, name)))
        # Risk and margin are computed as volume × tick_value/tick_size × price: a positive but
        # absurd tick_size (1e-320) would make that ratio overflow and raise mid-cycle.
        if not math.isfinite(self.tick_value / self.tick_size):
            raise ValueError("tick_value / tick_size must be a finite cash amount per price unit")
        if self.contract_size is not None:
            object.__setattr__(self, "contract_size", _positive(self.contract_size))
        if self.volume_min > self.volume_max:
            raise ValueError("volume_min cannot exceed volume_max")
        if self.volume_step > self.volume_min:
            raise ValueError("volume_step cannot exceed volume_min")
        if isinstance(self.digits, bool) or not isinstance(self.digits, int) \
                or not 0 <= self.digits <= 5:
            raise ValueError("digits must be an integer in [0, 5]")
        for name in ("spread_points", "stops_level_points"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    @classmethod
    def from_terminal(cls, info: Any, account_currency: str) -> "SymbolSpec":
        """Map a terminal ``SymbolInfo`` with strict types and no zero-defaults.

        A missing numeric field would otherwise become ``point=0.0`` and then fail inside the
        dataclass as a geometry error; here it names the symbol and the field instead.
        """
        name = getattr(info, "name", None)
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError("MT5 symbol_info returned no symbol name")
        numbers = {}
        for field_name in ("point", "trade_tick_size", "volume_min", "volume_max",
                           "volume_step"):
            value = _finite(getattr(info, field_name, None))
            if value is None:
                raise RuntimeError(f"MT5 symbol {name.strip().upper()} has no {field_name}")
            numbers[field_name] = value
        tick_value = _finite(getattr(info, "trade_tick_value_loss", None)) \
            or _finite(getattr(info, "trade_tick_value", None))
        if tick_value is None:
            raise RuntimeError(f"MT5 symbol {name.strip().upper()} has no trade_tick_value")
        digits = getattr(info, "digits", None)
        if isinstance(digits, bool) or not isinstance(digits, int):
            raise RuntimeError(f"MT5 symbol {name.strip().upper()} has no integer digits")
        integers = {}
        for field_name in ("spread", "trade_stops_level"):
            value = getattr(info, field_name, None)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RuntimeError(f"MT5 symbol {name.strip().upper()} has no {field_name}")
            integers[field_name] = value
        profit_currency = getattr(info, "currency_profit", None)
        if not isinstance(profit_currency, str) or not profit_currency.strip():
            raise RuntimeError(f"MT5 symbol {name.strip().upper()} has no profit currency")
        if profit_currency.strip().upper() != str(account_currency).strip().upper():
            raise RuntimeError(
                f"MT5 symbol {name.strip().upper()} pays profit in {profit_currency.strip().upper()},"
                f" not in the account currency {str(account_currency).strip().upper()}; sizing in"
                " account currency would be wrong")
        # A broker that does not fill this field reports 0.0, which is "unknown", not "free";
        # keeping it would divide the whole position's value by zero later. The tick ratio stays
        # as the fallback in that case.
        contract_size = _finite(getattr(info, "trade_contract_size", None))
        if contract_size is not None and contract_size <= 0:
            contract_size = None
        return cls(symbol=name, point=numbers["point"], digits=digits,
                   tick_size=numbers["trade_tick_size"], tick_value=tick_value,
                   volume_min=numbers["volume_min"], volume_max=numbers["volume_max"],
                   volume_step=numbers["volume_step"], spread_points=integers["spread"],
                   stops_level_points=integers["trade_stops_level"],
                   contract_size=contract_size)

    def price_to_volume(self, volume: float) -> float:
        """Round a requested volume down to the broker step, minimum and maximum lot."""
        steps = math.floor(max(0.0, volume) / self.volume_step + 1e-9)
        # The broker maximum is itself not guaranteed to be a whole number of steps; clip the
        # step count, not the product, so the result can never be an unsendable lot size.
        steps = min(steps, math.floor(self.volume_max / self.volume_step + 1e-9))
        rounded = steps * self.volume_step
        if rounded < self.volume_min - 1e-12:
            return 0.0
        return round(rounded, 8)

    @property
    def cash_per_price_unit(self) -> float:
        """Account-currency value of a one-unit move of the price for one lot.

        The contract size is definitional - an ounce of gold is an ounce whatever a quote field
        says - and it is exact here because ``from_terminal`` refuses instruments whose profit
        currency differs from the account's, so no conversion factor is missing. The tick ratio
        is kept only for brokers that do not publish a contract size, and it is not trustworthy:
        measured on MetaQuotes-Demo, XAUUSD reports ``trade_tick_value_loss=0.1`` at
        ``trade_tick_size=0.01`` - 10 $ per price unit per lot - while the same terminal's
        ``order_calc_profit`` pays 4 480 $ for 1 lot over a 44.8-point move, i.e. 100 $. Risk
        and margin sized from the tick field came out ten times too small, and the lots ten
        times too big. Silver behaves the same way; FX pairs agree, which is why only metals
        were affected.
        """
        if self.contract_size is not None:
            return self.contract_size
        return self.tick_value / self.tick_size

    @property
    def cash_estimate_divergence(self) -> float | None:
        """Relative disagreement between the two cash estimates, or None if only one exists.

        Zero means the broker's fields agree. Ten (the MetaQuotes-Demo metals case) means the
        tick field describes a different lot basis than the contract does; callers that show
        risk to an operator should not present either number without saying which was used.
        """
        if self.contract_size is None:
            return None
        tick_based = self.tick_value / self.tick_size
        return abs(self.contract_size - tick_based) / self.contract_size

    def value_per_price_unit(self, volume: float) -> float:
        """Account-currency value of a one-unit move of the price for ``volume`` lots."""
        value = volume * self.cash_per_price_unit
        if not math.isfinite(value) or value < 0:
            raise ValueError("computed position value is not usable")
        return value

    def margin_requirement(self, volume: float, leverage: int, price: float) -> float:
        """Approximate the margin a broker blocks for ``volume`` lots at ``price``.

        ``value_per_price_unit`` is the notional value of a *one-unit* price move, not the
        position's notional: the broker reserves notional / leverage, and the notional scales
        with the instrument's price. Sizing at price 1.0 understates margin on every pair
        quoted well above it and overstates it below. Cross-quoted instruments can still
        differ; ``SymbolSpec.from_terminal`` refuses those instead of silently mis-sizing.
        """
        if isinstance(leverage, bool) or not isinstance(leverage, int) or leverage <= 0:
            raise ValueError("leverage must be a positive integer")
        if not math.isfinite(price) or price <= 0:
            raise ValueError("price must be positive and finite")
        needed = self.value_per_price_unit(volume) * price / leverage
        if not math.isfinite(needed) or needed < 0:
            raise ValueError("computed margin is not usable")
        return needed


@dataclass(frozen=True)
class Tick:
    symbol: str
    bid: float
    ask: float
    server_time: datetime
    server_utc_offset_seconds: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        bid, ask = _positive(self.bid), _positive(self.ask)
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)
        if ask < bid:
            raise ValueError("ask cannot be below bid")
        if self.server_time.tzinfo is None or self.server_time.tzinfo is not UTC:
            raise ValueError("tick server_time must be a UTC-aware datetime")
        server_offset(self.server_utc_offset_seconds)

    @property
    def spread_points(self) -> float:
        return (self.ask - self.bid)

    @property
    def reference(self) -> dict[str, float]:
        return {"BUY": self.ask, "SELL": self.bid}


@dataclass(frozen=True)
class PositionSnapshot:
    ticket: int
    symbol: str
    direction: str
    volume: float
    entry_price: float
    stop_loss: float
    take_profit: float
    open_time: datetime

    def __post_init__(self) -> None:
        if isinstance(self.ticket, bool) or self.ticket <= 0:
            raise ValueError("position ticket must be a positive integer")
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        direction = self.direction.strip().upper()
        if direction not in {"BUY", "SELL"}:
            raise ValueError("position direction must be BUY or SELL")
        object.__setattr__(self, "direction", direction)
        for name in ("volume", "entry_price"):
            object.__setattr__(self, name, _positive(getattr(self, name)))
        for name in ("stop_loss", "take_profit"):
            value = _finite(getattr(self, name))
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative or zero")
            object.__setattr__(self, name, 0.0 if value is None else value)
        if self.open_time.tzinfo is None or self.open_time.utcoffset() is None:
            raise ValueError("position open_time must be timezone-aware")


class MT5Backend(Protocol):
    """Order-free view of a terminal: account, symbols, quotes, bars and positions.

    ``positions()`` returns ``None`` when the terminal did not answer. An empty list
    means the account is genuinely flat, and the two must never be confused.

    The equity baselines are not part of this Protocol because their requirements differ.
    ``day_start_equity`` and ``peak_equity`` are optional properties read with ``getattr``:
    a backend may expose them, and the agent blocks with ``EQUITY_BASELINES_UNAVAILABLE``
    when they are missing or unusable. ``realized_daily_pnl`` is mandatory: the agent
    resolves it statically and invokes the descriptor directly, and a backend that lacks
    it, raises from it, or reports ``None``, a string, a bool or a non-finite number is
    blocked with ``EQUITY_BASELINES_UNAVAILABLE``. A terminal adapter that cannot
    reconstruct a baseline must not guess it.
    """

    def account(self) -> AccountInfo: ...

    def symbol_spec(self, symbol: str) -> SymbolSpec: ...

    def tick(self, symbol: str) -> Tick: ...

    def bars(self, symbol: str, timeframe: str, count: int) -> list[MarketBar]: ...

    def positions(self) -> list[PositionSnapshot] | None: ...

    def shutdown(self) -> None: ...


BACKEND_CONTRACT_METHODS = ("account", "symbol_spec", "tick", "bars", "positions", "shutdown")


def assert_backend_contract(backend: Any) -> None:
    """Fail at construction if a backend cannot honour the order-free contract.

    ``MT5Backend`` is a structural Protocol, so a class that is missing a method is only
    caught when that method is called — after the agent has already started deciding.
    """
    missing = [name for name in BACKEND_CONTRACT_METHODS
               if not callable(getattr(backend, name, None))]
    if missing:
        raise TypeError(f"backend {type(backend).__name__} is not an MT5Backend; missing "
                        f"{', '.join(missing)}")


def resolve_broker_symbol(requested: str, available: Sequence[str]) -> str:
    """Map a logical instrument name onto the one this broker actually lists.

    Retail MT5 servers rarely publish ``XAUUSD``: Exness, RoboForex, ITAM and AMarkets
    append an account-type suffix, so the same instrument is ``XAUUSDm``, ``XAUUSDc`` or
    ``XAUUSD.i``. Without a mapping a run refuses every symbol on a second broker and
    looks like a broken strategy rather than a naming difference.

    The mapping is deliberately narrow, because a wrong resolution trades a different
    contract:

    * only six-character instrument codes are candidates, so ``BTC`` cannot resolve to
      the US stock ticker ``BTCO`` that MetaQuotes-Demo also lists;
    * the suffix is at most three characters, which covers the observed account types;
    * an exact match always wins, and several suffix matches refuse instead of guessing
      which one this account is allowed to trade.

    Comparison ignores case, but the name handed back is the server's own spelling:
    ``symbol_select("XAUUSDM")`` fails on a broker that lists ``XAUUSDm``.
    """
    name = requested.strip().upper()
    listed = [str(symbol).strip() for symbol in available]
    exact = [row for row in listed if row.upper() == name]
    if exact:
        return exact[0]
    if len(name) != 6:
        raise RuntimeError(f"MT5 has no symbol {name} and {name} is too short to alias safely")
    matches = sorted({row for row in listed
                      if row.upper().startswith(name)
                      and 0 < len(row) - len(name) <= 3})
    if not matches:
        raise RuntimeError(f"MT5 has no symbol {name} and no suffixed variant of it")
    if len(matches) > 1:
        raise RuntimeError(f"MT5 offers several variants of {name}: {', '.join(matches)}; "
                           "refusing to guess which one this account trades")
    return matches[0]


class LiveMT5Backend:
    """Order-free adapter for an installed MetaTrader 5 terminal on Windows.

    ``symbol_spec()`` may call ``symbol_select`` once to add a missing symbol to Market
    Watch; that is the only call that changes terminal state.

    Credentials are passed straight to ``mt5.initialize`` and never stored on the
    instance, logged or returned by any method.
    """

    def __init__(self, *, server_utc_offset_seconds: int,
                 terminal_path: str | None = None, login: int | None = None,
                 password: str | None = None, server: str | None = None,
                 timeout_ms: int = 60_000) -> None:
        # Required, not defaulted to 0: a UTC+2 or UTC+7 broker read as UTC makes every tick,
        # bar and schedule window wrong, and no default can be right for every broker.
        if (login is None) != (password is None) or (login is None) != (server is None):
            raise ValueError("login, password and server must be provided together")
        if login is not None and (isinstance(login, bool) or login <= 0):
            raise ValueError("login must be a positive integer")
        if password is not None and not str(password).strip():
            raise ValueError("password must be non-empty")
        if server is not None and not str(server).strip():
            raise ValueError("server must be non-empty")
        if isinstance(timeout_ms, bool) or not 1_000 <= timeout_ms <= 600_000:
            raise ValueError("timeout_ms must be between 1000 and 600000")
        self._offset = server_offset(server_utc_offset_seconds)
        self._terminal_path = terminal_path
        try:
            module = importlib.import_module("MetaTrader5")
        except ImportError as exc:  # pragma: no cover - depends on host install
            raise RuntimeError(
                "MetaTrader5 Python package is required on the MT5 Windows host") from exc
        kwargs: dict[str, Any] = {"timeout": timeout_ms}
        if terminal_path:
            kwargs["path"] = terminal_path
        if login is not None:
            kwargs.update({"login": int(login), "password": str(password), "server": str(server)})
        if not module.initialize(**kwargs):
            error = module.last_error()
            code = int(error[0]) if isinstance(error, Sequence) and error else -1
            # last_error() text can echo connection details; only the code is re-raised.
            raise RuntimeError(f"MT5 initialize failed (code={code})")
        self._mt5 = module

    def _to_utc(self, stamp: float, context: str) -> datetime:
        seconds = stamp / 1000.0 if stamp > 1e12 else stamp
        converted = datetime.fromtimestamp(seconds, tz=UTC) - timedelta(seconds=self._offset)
        if converted > datetime.now(UTC) + _MAX_FUTURE_SKEW:
            raise RuntimeError(
                f"MT5 {context} timestamp is more than"
                f" {int(_MAX_FUTURE_SKEW.total_seconds())}s in the future after subtracting"
                f" {self._offset}s; check server_utc_offset_seconds against the broker server")
        return converted

    def _offset_datetime(self, value: Any, context: str) -> datetime:
        stamp = _finite(getattr(value, "time_msc", None))
        if stamp is None or stamp <= 0:
            stamp = _finite(getattr(value, "time", None))
        if stamp is None or stamp <= 0:
            raise RuntimeError(f"MT5 returned no usable timestamp for {context}")
        return self._to_utc(stamp, context)

    @property
    def server_utc_offset_seconds(self) -> int:
        """The broker-server offset this adapter subtracts from every terminal stamp."""
        return self._offset

    def account(self) -> AccountInfo:
        info = self._mt5.account_info()
        if info is None:
            raise RuntimeError("MT5 account_info() returned no account")
        return AccountInfo.from_terminal(info)

    # Internal keys are upper case, but MT5 symbol names are not: Exness lists gold as
    # XAUUSDm and ``symbol_info("XAUUSDM")`` returns nothing. A run that resolved a broker's
    # own spelling registers it here so every terminal call below can go out under it.
    _names: dict[str, str] | None = None

    def use_broker_names(self, mapping: Mapping[str, str]) -> None:
        """Remember how this server spells the instruments the run asked for."""
        names = dict(self._names or {})
        for requested, exact in dict(mapping).items():
            key = str(requested).strip().upper()
            exact = str(exact).strip()
            held = names.get(key)
            if held is not None and held != exact:
                raise RuntimeError(f"{key} cannot name both {held} and {exact} on one terminal")
            names[key] = exact
        self._names = names

    def _name(self, symbol: str) -> str:
        return (self._names or {}).get(str(symbol).strip().upper(),
                                       str(symbol).strip().upper())

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        name = self._name(symbol)
        info = self._mt5.symbol_info(name)
        if info is None:
            # A symbol absent from Market Watch has no data. symbol_select is the documented
            # way to subscribe, and it does mutate the terminal's symbol list.
            if not self._mt5.symbol_select(name, True):
                raise RuntimeError(f"MT5 could not select symbol {name}")
            info = self._mt5.symbol_info(name)
        if info is None:
            raise RuntimeError(f"MT5 symbol_info() returned nothing for {name}")
        return SymbolSpec.from_terminal(info, self.account().currency)

    def tick(self, symbol: str) -> Tick:
        name = self._name(symbol)
        tick = self._mt5.symbol_info_tick(name)
        if tick is None:
            raise RuntimeError(f"MT5 returned no tick for {name}")
        bid = _finite(getattr(tick, "bid"))
        ask = _finite(getattr(tick, "ask"))
        if not bid or not ask:
            raise RuntimeError(f"MT5 has no live quote for {name} yet (bid/ask are zero)")
        return Tick(symbol=name, bid=bid, ask=ask,
                    server_time=self._offset_datetime(tick, f"{name} tick"),
                    server_utc_offset_seconds=self._offset)

    def bars(self, symbol: str, timeframe: str, count: int) -> list[MarketBar]:
        name = self._name(symbol)
        tf = timeframe.strip().upper()
        if tf not in _TIMEFRAME_CONSTANTS:
            raise ValueError(f"unsupported timeframe: {tf}")
        if isinstance(count, bool) or not 10 <= count <= 100_000:
            raise ValueError("count must be between 10 and 100000")
        constant = getattr(self._mt5, _TIMEFRAME_CONSTANTS[tf], None)
        if constant is None:
            raise RuntimeError(f"installed MetaTrader5 package lacks {tf}")
        # Position 1 is the most recent *closed* bar; position 0 is the bar still forming, whose
        # close repaints until the period ends and would let a signal be computed twice.
        rows = normalize_rate_rows(self._mt5.copy_rates_from_pos(name, constant, 1, count))
        if len(rows) != count:
            # A short answer is real on a freshly listed symbol or a thin history. Silently
            # trading a shorter window than the policy asked for would move every indicator.
            raise RuntimeError(f"MT5 returned {len(rows)} closed bars for {name},"
                               f" policy asked for {count}")
        bars = []
        for row in rows:
            missing = [key for key in ("time", "open", "high", "low", "close")
                       if row.get(key) is None]
            if missing:
                raise RuntimeError(f"MT5 rate row for {name} lacks {', '.join(missing)}")
            stamp = _finite(row["time"])
            if stamp is None or stamp <= 0:
                raise RuntimeError(f"MT5 returned no usable bar timestamp for {name}")
            bars.append(MarketBar(
                symbol=name, timeframe=tf, time=self._to_utc(stamp, f"{name} bar"),
                open=row["open"], high=row["high"], low=row["low"], close=row["close"],
                tick_volume=int(row.get("tick_volume", 0) or 0),
                spread_points=int(row.get("spread", 0) or 0),
                source="MetaTrader5.copy_rates_from_pos"))
        return validate_bar_series(bars, symbol=name, timeframe=tf)

    def positions(self) -> list[PositionSnapshot] | None:
        rows = self._mt5.positions_get()
        if rows is None:
            # The terminal did not answer. Returning [] here would read as "account is flat"
            # and let the position cap pass on a machine whose terminal is broken.
            return None
        positions = []
        for row in rows:
            missing = [field_name for field_name in
                       ("ticket", "symbol", "type", "volume", "price_open", "sl", "tp")
                       if getattr(row, field_name, None) is None]
            if missing:
                raise RuntimeError(f"MT5 position row is missing {', '.join(missing)}")
            code = int(row.type)
            if code not in (0, 1):
                raise RuntimeError(f"MT5 returned an unknown position type {code}")
            positions.append(PositionSnapshot(
                ticket=int(row.ticket), symbol=str(row.symbol),
                direction="BUY" if code == 0 else "SELL", volume=float(row.volume),
                entry_price=float(row.price_open), stop_loss=float(row.sl),
                take_profit=float(row.tp),
                open_time=self._offset_datetime(row, f"{str(row.symbol).upper()} position")))
        return positions

    def shutdown(self) -> None:
        self._mt5.shutdown()


def bars_frame(bars: Iterable[MarketBar]) -> list[MarketBar]:
    rows = list(bars)
    if not rows:
        raise ValueError("no bars supplied")
    validate_bar_series(rows)
    return rows
