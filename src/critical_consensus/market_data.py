"""Order-free MetaTrader 5 market data adapters (no order/trading calls).

No order/trading APIs are used in this module. MT5 server timestamps in CSV exports
are naive by default; callers must explicitly name their server timezone rather than
silently pretending it is UTC.
"""
from __future__ import annotations

import csv
import importlib
import math
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
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


def server_offset(value: Any, *, name: str = "server_utc_offset_seconds") -> int:
    """MT5 reports broker wall-clock time; the offset must be explicit, never guessed.

    One helper for the policy, the agent adapter and the history importer, so a tick, a bar
    and a schedule window can never be read against two different clocks.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer number of seconds")
    if value % 60 != 0 or abs(value) > 14 * 3600:
        raise ValueError(f"{name} must be whole minutes within +/-14 hours")
    return value


def normalize_rate_rows(rows: Any) -> list[dict[str, Any]]:
    """Turn whatever ``copy_rates_*`` returned into plain dictionaries.

    The package returns a numpy structured ndarray (rows are ``numpy.void`` objects with no
    ``.get``) or a pandas DataFrame (iterating it yields column *names*, not rows), so a bare
    ``dict(item)`` either raises or produces the wrong shape on a real terminal.
    """
    if rows is None:
        return []
    if hasattr(rows, "to_dict") and hasattr(rows, "columns"):
        return [dict(row) for row in rows.to_dict("records")]
    names = getattr(getattr(rows, "dtype", None), "names", None)
    if names is not None:
        return [{key: row[index] for index, key in enumerate(names)} for row in rows]
    return [dict(row) for row in rows]


def _last_error_code(mt5: Any) -> str:
    """Only the numeric MT5 error code: the library text is broker-localised and unbounded."""
    try:
        return str(int(mt5.last_error()[0]))
    except Exception:
        return "unknown"


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
        # A zero or negative quote breaks every ratio downstream (returns divide by the prior
        # close) and is never a real FX bar; refuse it at the boundary rather than propagate.
        if any(value <= 0 for value in values.values()):
            raise ValueError("bar prices must be positive")
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


def _source_zone(source_timezone: str | tzinfo) -> tzinfo:
    if isinstance(source_timezone, tzinfo):
        return source_timezone
    if not isinstance(source_timezone, str) or not source_timezone.strip():
        raise ValueError("source_timezone must be a timezone name or tzinfo")
    name = source_timezone.strip()
    if name.upper() in {"UTC", "ETC/UTC", "GMT", "Z"}:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"unknown source timezone: {name}; install the 'tzdata' package or pass a tzinfo object"
        ) from exc


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
        parsed = parsed.replace(tzinfo=_source_zone(source_timezone))
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
    server_utc_offset_seconds: int, terminal_path: str | None = None,
) -> list[MarketBar]:
    """Fetch historical rates from an already-configured MT5 terminal; no order API.

    ``symbol_select`` runs only to make the symbol visible to ``copy_rates_range``; it
    changes Market Watch, never account or order state.

    ``copy_rates_range`` stamps each bar in the broker's server time and gives no zone, so
    the offset is required: reading a UTC+2 broker as UTC would shift every bar by two hours
    and silently break every comparison against UTC.
    """
    offset = server_offset(server_utc_offset_seconds)
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
        raise RuntimeError(f"MT5 initialize failed (error {_last_error_code(mt5)})")
    try:
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"MT5 could not select symbol {symbol}"
                               f" (error {_last_error_code(mt5)})")
        tf_constant = getattr(mt5, _MT5_TIMEFRAME_NAMES[timeframe], None)
        if tf_constant is None:
            raise RuntimeError(f"installed MT5 package does not expose timeframe {timeframe}")
        rates = mt5.copy_rates_range(symbol, tf_constant, start, end)
        if rates is None:
            raise RuntimeError(f"MT5 copy_rates_range failed (error {_last_error_code(mt5)})")
        output = []
        for row in normalize_rate_rows(rates):
            stamp = row.get("time")
            if stamp is None:
                raise RuntimeError("MT5 returned a rate row without a time field")
            output.append(MarketBar(
                symbol=symbol, timeframe=timeframe,
                time=datetime.fromtimestamp(int(stamp) - offset, tz=UTC),
                open=row["open"], high=row["high"], low=row["low"], close=row["close"],
                tick_volume=int(row["tick_volume"]) if "tick_volume" in row else None,
                spread_points=int(row["spread"]) if "spread" in row else None,
                source="MetaTrader5.copy_rates_range",
            ))
        return validate_bar_series(output, symbol=symbol, timeframe=timeframe)
    finally:
        mt5.shutdown()


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
