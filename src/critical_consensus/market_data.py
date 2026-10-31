"""Read-only MetaTrader 5 market data adapters.

No order/trading APIs are used in this module. MT5 server timestamps in CSV exports
are naive by default; callers must explicitly name their server timezone rather than
silently pretending it is UTC.
"""
from __future__ import annotations

import csv
import importlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from statistics import pstdev
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
SUPPORTED_TIMEFRAMES = {"M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"}


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("bar timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _finite_price(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


@dataclass(frozen=True)
class MarketBar:
    symbol: str
    timeframe: str
    time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int | None = None
    spread_points: int | None = None
    source: str = "mt5"

    def __post_init__(self) -> None:
        symbol = str(self.symbol).strip().upper()
        timeframe = str(self.timeframe).strip().upper()
        if not symbol or len(symbol) > 64:
            raise ValueError("symbol must be a non-empty identifier of at most 64 characters")
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        stamp = _as_utc(self.time)
        values = {name: _finite_price(getattr(self, name), name)
                  for name in ("open", "high", "low", "close")}
        if values["high"] < max(values["open"], values["close"], values["low"]):
            raise ValueError("high must be >= open, close and low")
        if values["low"] > min(values["open"], values["close"], values["high"]):
            raise ValueError("low must be <= open, close and high")
        for name in ("tick_volume", "spread_points"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer or null")
        if not str(self.source).strip():
            raise ValueError("source must be non-empty")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "timeframe", timeframe)
        object.__setattr__(self, "time", stamp)
        for name, value in values.items():
            object.__setattr__(self, name, value)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "timeframe": self.timeframe, "time": self.time.isoformat(),
            "open": self.open, "high": self.high, "low": self.low, "close": self.close,
            "tick_volume": self.tick_volume, "spread_points": self.spread_points, "source": self.source,
        }


def validate_bar_series(
    bars: Iterable[MarketBar], *, symbol: str | None = None, timeframe: str | None = None,
    min_bars: int = 1,
) -> list[MarketBar]:
    if isinstance(min_bars, bool) or min_bars < 1:
        raise ValueError("min_bars must be >= 1")
    rows = list(bars)
    if len(rows) < min_bars:
        raise ValueError(f"insufficient bars: need {min_bars}, received {len(rows)}")
    expected_symbol = symbol.strip().upper() if symbol else (rows[0].symbol if rows else "")
    expected_tf = timeframe.strip().upper() if timeframe else (rows[0].timeframe if rows else "")
    previous: datetime | None = None
    for bar in rows:
        if not isinstance(bar, MarketBar):
            raise ValueError("bar series contains a non-MarketBar record")
        if bar.symbol != expected_symbol or bar.timeframe != expected_tf:
            raise ValueError("bar series mixes symbols or timeframes")
        if previous is not None and bar.time <= previous:
            raise ValueError("bar timestamps must be strictly increasing and unique")
        previous = bar.time
    return rows


def _normalise_header(value: str) -> str:
    return "".join(ch for ch in value.lstrip("\ufeff").strip().lower() if ch.isalnum())


def _find(row: Mapping[str, str], names: Sequence[str], *, required: bool = True) -> str | None:
    normalised = {_normalise_header(key): value for key, value in row.items() if key is not None}
    for name in names:
        key = _normalise_header(name)
        if key in normalised and normalised[key] not in (None, ""):
            return normalised[key].strip()
    if required:
        raise ValueError(f"missing required CSV column: {names[0]}")
    return None


def _parse_datetime(raw: str, source_timezone: str | tzinfo | None) -> datetime:
    value = raw.strip()
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    if parsed is None:
        for fmt in (
            "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M",
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
            "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M",
        ):
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise ValueError(f"unrecognised timestamp: {value!r}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if source_timezone is None:
            raise ValueError("naive MT5 export timestamps require an explicit source_timezone")
        try:
            zone = ZoneInfo(source_timezone) if isinstance(source_timezone, str) else source_timezone
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown source timezone: {source_timezone}") from exc
        if zone is None:
            raise ValueError("source_timezone must be a timezone name or tzinfo")
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(UTC)


def read_mt5_csv(
    path: str | Path, *, symbol: str, timeframe: str,
    source_timezone: str | tzinfo | None = None,
) -> list[MarketBar]:
    """Read MT5's common Date,Time,OHLC,TickVol,Volume,Spread export format."""
    output: list[MarketBar] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("CSV must have a header row")
        for line_number, row in enumerate(reader, start=2):
            try:
                raw_stamp = _find(row, ("datetime", "timestamp", "date_time"), required=False)
                if raw_stamp is None:
                    date = _find(row, ("date",))
                    clock = _find(row, ("time",))
                    raw_stamp = f"{date} {clock}"
                tick_volume = _find(row, ("tickvol", "tick_volume", "tickvolume"), required=False)
                if tick_volume is None:
                    tick_volume = _find(row, ("volume", "vol"), required=False)
                spread = _find(row, ("spread", "spread_points"), required=False)
                output.append(MarketBar(
                    symbol=symbol, timeframe=timeframe,
                    time=_parse_datetime(raw_stamp, source_timezone),
                    open=_find(row, ("open",)), high=_find(row, ("high",)),
                    low=_find(row, ("low",)), close=_find(row, ("close",)),
                    tick_volume=int(tick_volume) if tick_volume not in (None, "") else None,
                    spread_points=int(spread) if spread not in (None, "") else None,
                    source=f"mt5-csv:{Path(path).name}",
                ))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"invalid MT5 CSV row {line_number}: {exc}") from exc
    return validate_bar_series(output, symbol=symbol, timeframe=timeframe)


_MT5_TIMEFRAME_NAMES = {
    "M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30", "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1", "W1": "TIMEFRAME_W1", "MN1": "TIMEFRAME_MN1",
}


def fetch_mt5_bars(
    symbol: str, timeframe: str, start_utc: datetime, end_utc: datetime, *,
    terminal_path: str | None = None,
) -> list[MarketBar]:
    """Fetch historical rates from an already-configured MT5 terminal; read-only only."""
    symbol = symbol.strip().upper()
    timeframe = timeframe.strip().upper()
    if timeframe not in _MT5_TIMEFRAME_NAMES:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    start, end = _as_utc(start_utc), _as_utc(end_utc)
    if start >= end:
        raise ValueError("start_utc must be earlier than end_utc")
    try:
        mt5 = importlib.import_module("MetaTrader5")
    except ImportError as exc:
        raise RuntimeError("MetaTrader5 Python package is required on the MT5 Windows host") from exc
    initialized = mt5.initialize(path=terminal_path) if terminal_path else mt5.initialize()
    if not initialized:
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"MT5 could not select symbol {symbol}: {mt5.last_error()}")
        tf_constant = getattr(mt5, _MT5_TIMEFRAME_NAMES[timeframe], None)
        if tf_constant is None:
            raise RuntimeError(f"installed MT5 package does not expose timeframe {timeframe}")
        rates = mt5.copy_rates_range(symbol, tf_constant, start, end)
        if rates is None:
            raise RuntimeError(f"MT5 copy_rates_range failed: {mt5.last_error()}")
        output = []
        for item in rates:
            row = dict(item)
            output.append(MarketBar(
                symbol=symbol, timeframe=timeframe,
                time=datetime.fromtimestamp(int(row["time"]), tz=UTC),
                open=row["open"], high=row["high"], low=row["low"], close=row["close"],
                tick_volume=int(row["tick_volume"]) if "tick_volume" in row else None,
                spread_points=int(row["spread"]) if "spread" in row else None,
                source="MetaTrader5.copy_rates_range",
            ))
        return validate_bar_series(output, symbol=symbol, timeframe=timeframe)
    finally:
        mt5.shutdown()




_FORECAST_BAR_DURATIONS = {
    "M1": timedelta(minutes=1),
    "M5": timedelta(minutes=5),
    "M15": timedelta(minutes=15),
    "M30": timedelta(minutes=30),
    "H1": timedelta(hours=1),
    "H4": timedelta(hours=4),
    "D1": timedelta(days=1),
    "W1": timedelta(days=7),
}


def forecast_bar_duration(timeframe: str) -> timedelta:
    """Return the fixed close interval used by the objective next-bar target policy."""
    key = str(timeframe).strip().upper()
    try:
        return _FORECAST_BAR_DURATIONS[key]
    except KeyError as exc:
        raise ValueError(
            "objective next-bar forecast policy does not support this timeframe"
        ) from exc


def retain_completed_bars(
    bars: Iterable[MarketBar], *, now: datetime | None = None,
    finalization_grace_seconds: float = 2.0, min_bars: int = 21,
) -> list[MarketBar]:
    """Exclude a still-forming MT5 bar before building a reference forecast snapshot."""
    rows = validate_bar_series(bars, min_bars=1)
    current = _as_utc(now or datetime.now(UTC))
    if (isinstance(finalization_grace_seconds, bool)
            or not isinstance(finalization_grace_seconds, (int, float))
            or not math.isfinite(float(finalization_grace_seconds))
            or not 0 <= float(finalization_grace_seconds) <= 60):
        raise ValueError("finalization_grace_seconds must be finite and in [0,60]")
    grace = timedelta(seconds=float(finalization_grace_seconds))
    duration = forecast_bar_duration(rows[-1].timeframe)
    completed = [bar for bar in rows if bar.time + duration + grace <= current]
    if len(completed) < min_bars:
        raise ValueError(
            f"insufficient completed bars for forecast snapshot: need {min_bars}, received {len(completed)}"
        )
    return completed


def build_next_bar_forecast_contract(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Specify the binary event independently of each agent's subjective verdict confidence."""
    if not isinstance(snapshot, Mapping):
        raise ValueError("market snapshot must be an object")
    symbol = str(snapshot.get("symbol", "")).strip().upper()
    timeframe = str(snapshot.get("timeframe", "")).strip().upper()
    reference_at = snapshot.get("as_of")
    reference_close = _finite_price(snapshot.get("last_close"), "reference_close")
    if not symbol or not reference_at or reference_close <= 0:
        raise ValueError("market snapshot cannot define a next-bar forecast target")
    parsed_reference_at = datetime.fromisoformat(str(reference_at).replace("Z", "+00:00"))
    if parsed_reference_at.tzinfo is None or parsed_reference_at.utcoffset() is None:
        raise ValueError("forecast reference timestamp must include a timezone")
    forecast_bar_duration(timeframe)
    return {
        "target_policy_version": "next-available-bar-close-above-reference-v1",
        "target_event": "NEXT_AVAILABLE_BAR_CLOSE_ABOVE_REFERENCE",
        "symbol": symbol,
        "timeframe": timeframe,
        "reference_at": parsed_reference_at.astimezone(UTC).isoformat(),
        "reference_close": reference_close,
        "definition": (
            "Outcome is 1 only when the close of the first persisted market bar whose "
            "bar_time is strictly after the individual agent issue timestamp is greater "
            "than reference_close; flat or lower closes are 0. A bar already opened before "
            "the agent issued its forecast is never used. The target bar must be complete."
        ),
        "provenance_limit": (
            "Resolution proves deterministic agreement with an immutable locally persisted "
            "market bar; it does not independently authenticate broker-feed completeness or accuracy."
        ),
    }


def build_market_snapshot(bars: Iterable[MarketBar], *, min_bars: int = 21) -> dict[str, Any]:
    """Compute transparent descriptive features; this is not a trade recommendation."""
    rows = validate_bar_series(bars, min_bars=min_bars)
    closes = [bar.close for bar in rows]
    highs, lows = [bar.high for bar in rows], [bar.low for bar in rows]
    returns = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes)) if closes[i - 1] != 0]
    if len(returns) < 20:
        raise ValueError("at least 21 valid closes are required for the 20-period volatility feature")
    true_ranges = []
    for i in range(1, len(rows)):
        true_ranges.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
    volatility = pstdev(returns[-20:]) if len(returns) >= 2 else 0.0
    sma5 = sum(closes[-5:]) / 5
    sma20 = sum(closes[-20:]) / 20
    if closes[-21] == 0:
        raise ValueError("20-period return cannot be calculated from a zero prior close")
    return {
        "symbol": rows[-1].symbol, "timeframe": rows[-1].timeframe,
        "as_of": rows[-1].time.isoformat(), "bar_count": len(rows),
        "last_close": closes[-1], "return_1": closes[-1] / closes[-2] - 1.0,
        "return_5": closes[-1] / closes[-6] - 1.0,
        "return_20": closes[-1] / closes[-21] - 1.0,
        "sma_5": sma5, "sma_20": sma20, "sma_5_minus_sma_20": sma5 - sma20,
        "atr_14": sum(true_ranges[-14:]) / 14,
        "realized_volatility_20": volatility,
        "evidence_note": "Descriptive read-only features; not a calibrated forecast or trade authorization.",
    }
