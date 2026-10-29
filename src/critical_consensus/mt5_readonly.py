"""Read-only adapter for MetaTrader 5 quote snapshots.

The adapter deliberately exposes no order submission or account mutation methods.
A valid snapshot is input for analysis only and never implies that trading is allowed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import importlib
import math
import uuid
from typing import Any, Callable, Sequence

from .market_data import validate_market_snapshot


class MT5ReadOnlyError(RuntimeError):
    """Read-only terminal adapter error."""


def _utc_string(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_price(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


class MT5ReadOnlyAdapter:
    """Allow-listed quote reader; no order_send, trade request, or terminal mutation calls."""

    def __init__(
        self,
        client: Any,
        *,
        source_id: str,
        allowed_symbols: Sequence[str],
        max_age_seconds: float,
        future_skew_seconds: float,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if client is None:
            raise ValueError("MT5 client is required")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id is required")
        if (not isinstance(allowed_symbols, Sequence) or isinstance(allowed_symbols, (str, bytes))
                or not allowed_symbols or any(not isinstance(item, str) or not item.strip() for item in allowed_symbols)):
            raise ValueError("allowed_symbols must be a non-empty sequence of symbols")
        if len(set(allowed_symbols)) != len(allowed_symbols):
            raise ValueError("allowed_symbols must be unique")
        if (isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float))
                or not math.isfinite(float(max_age_seconds)) or not 0 < max_age_seconds <= 60):
            raise ValueError("max_age_seconds must be finite and in 0..60")
        if (isinstance(future_skew_seconds, bool) or not isinstance(future_skew_seconds, (int, float))
                or not math.isfinite(float(future_skew_seconds)) or not 0 <= future_skew_seconds <= 2):
            raise ValueError("future_skew_seconds must be finite and in 0..2")
        self._client = client
        self._source_id = source_id.strip()
        self._allowed_symbols = frozenset(allowed_symbols)
        self._max_age_seconds = float(max_age_seconds)
        self._future_skew_seconds = float(future_skew_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._connected = False

    @classmethod
    def from_installed(
        cls,
        *,
        source_id: str,
        allowed_symbols: Sequence[str],
        max_age_seconds: float,
        future_skew_seconds: float,
        clock: Callable[[], datetime] | None = None,
    ) -> "MT5ReadOnlyAdapter":
        """Load the optional MetaTrader5 Python package only when explicitly requested."""
        try:
            client = importlib.import_module("MetaTrader5")
        except ImportError as exc:
            raise MT5ReadOnlyError("MetaTrader5 Python package is not installed in this environment") from exc
        return cls(client, source_id=source_id, allowed_symbols=allowed_symbols,
                   max_age_seconds=max_age_seconds, future_skew_seconds=future_skew_seconds, clock=clock)

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        if self._connected:
            return
        try:
            connected = self._client.initialize()
        except Exception as exc:
            raise MT5ReadOnlyError("MT5 terminal initialization failed") from exc
        if connected is not True:
            raise MT5ReadOnlyError("MT5 terminal initialization was not confirmed")
        self._connected = True

    def close(self) -> None:
        if not self._connected:
            return
        try:
            self._client.shutdown()
        except Exception as exc:
            raise MT5ReadOnlyError("MT5 terminal shutdown failed") from exc
        finally:
            self._connected = False

    def read_snapshot(self, symbol: str) -> dict[str, Any]:
        if not self._connected:
            raise MT5ReadOnlyError("MT5 terminal is not connected")
        if not isinstance(symbol, str) or symbol not in self._allowed_symbols:
            return {"status": "REJECTED", "valid": False, "blockers": ["SYMBOL_NOT_ALLOWLISTED"],
                    "snapshot": None, "trade_allowed": False}
        try:
            tick = self._client.symbol_info_tick(symbol)
        except Exception:
            return {"status": "REJECTED", "valid": False, "blockers": ["MT5_TICK_READ_FAILED"],
                    "snapshot": None, "trade_allowed": False}
        if tick is None:
            return {"status": "REJECTED", "valid": False, "blockers": ["MT5_TICK_UNAVAILABLE"],
                    "snapshot": None, "trade_allowed": False}
        try:
            current = self._clock()
        except Exception:
            current = None
        if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
            current = None
            received_time = None
        else:
            current = current.astimezone(timezone.utc)
            received_time = _utc_string(current)
        raw_time_msc = getattr(tick, "time_msc", None)
        event_time = None
        if isinstance(raw_time_msc, int) and not isinstance(raw_time_msc, bool):
            try:
                event_time = _utc_string(datetime.fromtimestamp(raw_time_msc / 1000.0, timezone.utc))
            except (ValueError, TypeError, OverflowError, OSError):
                event_time = None
        snapshot = {
            "schema_version": 1,
            "snapshot_id": f"mt5-{uuid.uuid4().hex}",
            "source_id": self._source_id,
            "symbol": symbol,
            "bid": _safe_price(getattr(tick, "bid", None)),
            "ask": _safe_price(getattr(tick, "ask", None)),
            "event_time": event_time,
            "received_time": received_time,
        }
        validation = validate_market_snapshot(
            snapshot,
            max_age_seconds=self._max_age_seconds,
            future_skew_seconds=self._future_skew_seconds,
            now=current,
        )
        is_valid = validation.get("valid") is True
        return {
            "status": "VALID_READ_ONLY_SNAPSHOT" if is_valid else "REJECTED",
            "valid": is_valid,
            "blockers": list(validation.get("blockers", [])),
            "snapshot": snapshot,
            "validation": validation,
            "trade_allowed": False,
        }
