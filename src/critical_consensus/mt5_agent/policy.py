"""Fail-closed policy configuration for the MT5 agent."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..market_data import server_offset
from .codes import blackout_window_code
from .i18n import normalize_language

MODES = ("observe", "paper")
# "confirm" is the default: an agent that opens positions on its own is a decision the
# operator has to make explicitly, not something a missing key quietly grants.
EXECUTION_MODES = ("confirm", "auto")
STRATEGY_FAMILIES = ("sma", "ema", "bollinger", "donchian")
_MINUTE_SPAN = 24 * 60


def _number(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or isinstance(value, str):
        raise ValueError(f"{name} must be a JSON-style number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _fraction(value: Any, *, name: str, low: float, high: float) -> float:
    number = _number(value, name=name)
    if not low <= number <= high:
        raise ValueError(f"{name} must be in [{low}, {high}]")
    return number


def _integer(value: Any, *, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not low <= value <= high:
        raise ValueError(f"{name} must be in [{low}, {high}]")
    return value


def _boolean(value: Any, *, name: str) -> bool:
    """A safety switch may only be turned off by an explicit ``false``, never by a falsy value."""
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean, not {type(value).__name__}")
    return value


@dataclass(frozen=True)
class StrategyConfig:
    # The family decides which rule produces the direction; the ATR stop, the target
    # multiple and every geometry check are shared, so the variants differ only in how
    # they read the same closed bars.
    family: str = "sma"
    min_bars: int = 60
    sma_fast: int = 5
    sma_slow: int = 20
    ema_fast: int = 20
    ema_slow: int = 50
    bollinger_period: int = 20
    bollinger_dev: float = 2.0
    donchian_period: int = 20
    atr_period: int = 14
    stop_atr_multiple: float = 2.0
    take_profit_risk_multiple: float = 2.0
    min_sma_gap_atr: float = 0.10
    min_momentum_abs: float = 0.0
    max_confidence: float = 0.95

    def __post_init__(self) -> None:
        if self.family not in STRATEGY_FAMILIES:
            raise ValueError(f"strategy.family must be one of {sorted(STRATEGY_FAMILIES)}")
        _integer(self.min_bars, name="min_bars", low=30, high=100_000)
        _integer(self.sma_fast, name="sma_fast", low=2, high=500)
        _integer(self.sma_slow, name="sma_slow", low=5, high=5_000)
        if self.sma_fast >= self.sma_slow:
            raise ValueError("sma_fast must be smaller than sma_slow")
        _integer(self.ema_fast, name="ema_fast", low=2, high=500)
        _integer(self.ema_slow, name="ema_slow", low=5, high=5_000)
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be smaller than ema_slow")
        _integer(self.bollinger_period, name="bollinger_period", low=5, high=5_000)
        _fraction(self.bollinger_dev, name="bollinger_dev", low=0.5, high=6.0)
        _integer(self.donchian_period, name="donchian_period", low=5, high=5_000)
        # The warm-up has to cover the family's own longest window, not just the SMA one,
        # or a variant would be judged on a history it cannot compute.
        if self.min_bars <= self.lookback:
            raise ValueError(f"min_bars must exceed the {self.family} lookback {self.lookback}")
        _integer(self.atr_period, name="atr_period", low=2, high=500)
        _fraction(self.stop_atr_multiple, name="stop_atr_multiple", low=0.5, high=20.0)
        _fraction(self.take_profit_risk_multiple, name="take_profit_risk_multiple",
                  low=0.1, high=20.0)
        _fraction(self.min_sma_gap_atr, name="min_sma_gap_atr", low=0.0, high=10.0)
        _fraction(self.min_momentum_abs, name="min_momentum_abs", low=0.0, high=1.0)
        _fraction(self.max_confidence, name="max_confidence", low=0.0, high=0.99)

    @property
    def lookback(self) -> int:
        return {
            "sma": self.sma_slow,
            "ema": self.ema_slow,
            "bollinger": self.bollinger_period,
            "donchian": self.donchian_period,
        }[self.family]


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade_fraction: float = 0.005
    max_open_positions: int = 1
    max_daily_loss_fraction: float = 0.02
    max_drawdown_fraction: float = 0.06
    min_risk_reward: float = 1.5
    max_spread_points: int = 25
    max_quote_age_seconds: int = 15

    def __post_init__(self) -> None:
        _fraction(self.risk_per_trade_fraction, name="risk_per_trade_fraction",
                  low=0.0001, high=0.05)
        _integer(self.max_open_positions, name="max_open_positions", low=0, high=50)
        _fraction(self.max_daily_loss_fraction, name="max_daily_loss_fraction",
                  low=0.0001, high=0.25)
        _fraction(self.max_drawdown_fraction, name="max_drawdown_fraction", low=0.001, high=0.5)
        _fraction(self.min_risk_reward, name="min_risk_reward", low=0.5, high=50.0)
        _integer(self.max_spread_points, name="max_spread_points", low=0, high=10_000)
        _integer(self.max_quote_age_seconds, name="max_quote_age_seconds", low=1, high=300)
        if self.max_daily_loss_fraction > self.max_drawdown_fraction:
            raise ValueError("max_daily_loss_fraction cannot exceed max_drawdown_fraction")
        planned = self.risk_per_trade_fraction * self.max_open_positions
        if planned > self.max_daily_loss_fraction + 1e-12:
            raise ValueError(
                f"risk_per_trade_fraction * max_open_positions = {planned:.6f} exceeds"
                f" max_daily_loss_fraction = {self.max_daily_loss_fraction:.6f};"
                " the caps would contradict each other")


@dataclass(frozen=True)
class TradingWindow:
    start_minute: int = 0
    end_minute: int = 1_320
    blocked_weekdays: tuple[int, ...] = (6,)

    def __post_init__(self) -> None:
        _integer(self.start_minute, name="start_minute", low=0, high=_MINUTE_SPAN)
        _integer(self.end_minute, name="end_minute", low=0, high=_MINUTE_SPAN)
        if self.start_minute == self.end_minute:
            raise ValueError("trading window start and end cannot be equal (empty window)")
        if isinstance(self.blocked_weekdays, (str, bytes)) or not isinstance(
                self.blocked_weekdays, Iterable):
            raise ValueError("blocked_weekdays must be a list of integers, Monday=0")
        days = tuple(sorted({
            _integer(day, name="blocked_weekday", low=0, high=6)
            for day in self.blocked_weekdays}))
        object.__setattr__(self, "blocked_weekdays", days)

    def contains(self, moment: datetime) -> bool:
        """Half-open window [start, end) in UTC minutes of the day; naive moments are refused."""
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("trading window check requires a timezone-aware UTC moment")
        if moment.weekday() in self.blocked_weekdays:
            return False
        utc = moment.astimezone(timezone.utc)
        minute = utc.hour * 60 + utc.minute
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute < self.end_minute
        return minute >= self.start_minute or minute < self.end_minute


@dataclass(frozen=True)
class AgentPolicy:
    version: int
    mode: str
    symbol: str
    timeframe: str
    history_bars: int
    server_utc_offset_seconds: int
    expected_login: int | None
    expected_server: str | None
    require_demo: bool
    risk: RiskLimits = field(default_factory=RiskLimits)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    window: TradingWindow = field(default_factory=TradingWindow)
    blackout_windows: tuple[tuple[datetime, datetime], ...] = ()
    kill_switch_active: bool = True
    kill_switch_reason: str = "POLICY_NOT_PROVIDED"
    symbol_spec: Mapping[str, Any] | None = None
    execution_mode: str = "confirm"
    ui_language: str = "ru"

    def __post_init__(self) -> None:
        _integer(self.version, name="version", low=1, high=10)
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}; live execution is not implemented")
        if not isinstance(self.symbol, str):
            raise ValueError("symbol must be a string")
        symbol = self.symbol.strip().upper()
        if not symbol or len(symbol) > 64:
            raise ValueError("symbol must be a non-empty identifier of at most 64 characters")
        object.__setattr__(self, "symbol", symbol)
        timeframe = str(self.timeframe).strip().upper()
        from ..market_data import SUPPORTED_TIMEFRAMES
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        object.__setattr__(self, "timeframe", timeframe)
        _integer(self.history_bars, name="history_bars", low=30, high=100_000)
        if self.history_bars < self.strategy.min_bars:
            raise ValueError("history_bars must be at least strategy.min_bars")
        if self.strategy.atr_period >= self.strategy.min_bars:
            # ATR over the last atr_period ranges silently shrinks to min_bars-1 ranges when the
            # series is shorter, so the configured window would never be the one averaged.
            raise ValueError("strategy.atr_period must be smaller than strategy.min_bars")
        server_offset(self.server_utc_offset_seconds)
        if self.expected_login is not None:
            _integer(self.expected_login, name="expected_login", low=1, high=2 ** 62)
        if self.expected_server is not None and not str(self.expected_server).strip():
            raise ValueError("expected_server must be non-empty or null")
        _boolean(self.require_demo, name="require_demo")
        execution = str(self.execution_mode).strip().lower()
        if execution not in EXECUTION_MODES:
            raise ValueError(f"execution_mode must be one of {EXECUTION_MODES}; got "
                             f"{self.execution_mode!r}")
        object.__setattr__(self, "execution_mode", execution)
        if execution == "auto" and not self.require_demo:
            # require_demo: false is the one knob that would let this policy drive a real
            # account, and automatic execution must never reach it.
            raise ValueError("execution_mode 'auto' requires account.require_demo: true;"
                             " unattended execution against a real account is not allowed")
        object.__setattr__(self, "ui_language", normalize_language(self.ui_language))
        _boolean(self.kill_switch_active, name="kill_switch_active")
        if self.strategy.take_profit_risk_multiple < self.risk.min_risk_reward:
            # A proposal's reward/risk is exactly take_profit_risk_multiple, so a minimum above
            # it would reject every signal the strategy can ever produce.
            raise ValueError(
                "strategy.take_profit_risk_multiple must be at least risk.min_risk_reward,"
                " otherwise no signal can ever pass the gate")
        if self.kill_switch_active and not str(self.kill_switch_reason).strip():
            raise ValueError("an active kill switch requires a reason")
        for start, end in self.blackout_windows:
            if start.tzinfo is None or end.tzinfo is None:
                raise ValueError("blackout windows must be timezone-aware")
            if end <= start:
                raise ValueError("blackout window end must be after start")
            if end - start > timedelta(days=30):
                raise ValueError("blackout window cannot exceed 30 days")
        if self.symbol_spec is not None:
            spec = self.symbol_specification()
            if spec.symbol != self.symbol:
                raise ValueError("symbol_spec.symbol must match the policy symbol")

    @property
    def confirmation_required(self) -> bool:
        """True when every new position needs an explicit operator approval first."""
        return self.execution_mode == "confirm"

    def symbol_specification(self):
        """Build the declared ``SymbolSpec``; used when no live terminal supplies one."""
        from .backend import SymbolSpec

        if self.symbol_spec is None:
            raise ValueError("policy has no symbol_spec section; read the specification "
                             "from the terminal instead")
        try:
            return SymbolSpec(**dict(self.symbol_spec))
        except TypeError as exc:
            # An unknown or missing key would otherwise escape as a traceback instead of the
            # CLI's clean, localized policy-rejection exit.
            raise ValueError(f"invalid symbol_spec: {exc}") from None

    def blackout_blocker(self, moment: datetime) -> str | None:
        for index, (start, end) in enumerate(self.blackout_windows):
            if start <= moment < end:
                return blackout_window_code(index + 1)
        return None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AgentPolicy":
        if not isinstance(raw, Mapping):
            raise ValueError("policy must be a mapping")
        unknown = set(raw) - {
            "version", "mode", "symbol", "timeframe", "history_bars",
            "server_utc_offset_seconds", "account", "risk", "strategy", "schedule",
            "kill_switch", "symbol_spec", "execution_mode", "ui_language",
        }
        if unknown:
            raise ValueError(f"unknown policy keys: {sorted(unknown)}")
        account = _section(raw, "account", {
            "expected_login", "expected_server", "require_demo"})
        risk = _section(raw, "risk", {
            "risk_per_trade_fraction", "max_open_positions", "max_daily_loss_fraction",
            "max_drawdown_fraction", "min_risk_reward", "max_spread_points",
            "max_quote_age_seconds"})
        strategy = _section(raw, "strategy", {
            "family", "min_bars", "sma_fast", "sma_slow", "ema_fast", "ema_slow",
            "bollinger_period", "bollinger_dev", "donchian_period", "atr_period",
            "stop_atr_multiple", "take_profit_risk_multiple", "min_sma_gap_atr",
            "min_momentum_abs", "max_confidence"})
        schedule = _section(raw, "schedule", {
            "window_start_minute_utc", "window_end_minute_utc", "blocked_weekdays_utc",
            "blackout_windows_utc"})
        switch = _section(raw, "kill_switch", {"active", "reason"})
        window_raw = {
            "start_minute": schedule.get("window_start_minute_utc", 0),
            "end_minute": schedule.get("window_end_minute_utc", 1_320),
            "blocked_weekdays": tuple(schedule.get("blocked_weekdays_utc", (6,))),
        }
        login = account.get("expected_login")
        server = account.get("expected_server")
        spec_raw = raw.get("symbol_spec")
        if spec_raw is not None and not isinstance(spec_raw, Mapping):
            raise ValueError("symbol_spec must be a mapping or null")
        if "server_utc_offset_seconds" not in raw or raw["server_utc_offset_seconds"] is None:
            # Defaulting to zero would read a UTC+2 or UTC+7 broker clock as UTC: every bar and
            # tick would land two or seven hours off and the trading window would be wrong.
            raise ValueError("server_utc_offset_seconds is required; declare the broker server"
                             " offset in whole seconds")
        return cls(
            version=_integer(raw.get("version", 1), name="version", low=1, high=10),
            mode=str(raw.get("mode", "paper")).strip().lower(),
            symbol=raw.get("symbol", ""),
            timeframe=str(raw.get("timeframe", "H1")),
            history_bars=_integer(raw.get("history_bars", 200), name="history_bars",
                                  low=30, high=100_000),
            server_utc_offset_seconds=server_offset(raw["server_utc_offset_seconds"]),
            expected_login=None if login is None else _integer(
                login, name="expected_login", low=1, high=2 ** 62),
            expected_server=None if server is None else str(server).strip(),
            require_demo=_boolean(account.get("require_demo", True), name="require_demo"),
            risk=RiskLimits(**risk),
            strategy=StrategyConfig(**strategy),
            window=TradingWindow(**window_raw),
            blackout_windows=_blackouts(schedule.get("blackout_windows_utc", ())),
            kill_switch_active=_boolean(switch.get("active", True), name="kill_switch.active"),
            kill_switch_reason=str(switch.get("reason", "POLICY_NOT_PROVIDED")),
            symbol_spec=dict(spec_raw) if spec_raw is not None else None,
            execution_mode=str(raw.get("execution_mode", "confirm")),
            ui_language=str(raw.get("ui_language", "ru")),
        )


def _section(raw: Mapping[str, Any], name: str, allowed: set[str]) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, Mapping):
        raise ValueError(f"policy section {name} must be a mapping")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown keys in policy section {name}: {sorted(unknown)}")
    return dict(value)


def _blackouts(raw: Any) -> tuple[tuple[datetime, datetime], ...]:
    if isinstance(raw, Mapping) or not isinstance(raw, (list, tuple)):
        raise ValueError("blackout_windows_utc must be a list of [start, end] pairs")
    parsed: list[tuple[datetime, datetime]] = []
    for item in raw:
        if isinstance(item, Mapping) or not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("each blackout window needs exactly a start and an end")
        pair = tuple(_aware_utc(stamp) for stamp in item)
        parsed.append((pair[0], pair[1]))
    return tuple(parsed)


def _aware_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("blackout window timestamps must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def load_policy(path: str | Path) -> AgentPolicy:
    import yaml

    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"policy file not found: {file_path}")
    raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("policy file must contain a YAML mapping")
    return AgentPolicy.from_mapping(raw)
