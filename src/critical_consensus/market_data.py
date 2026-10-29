"""Fail-closed validation for normalized, read-only market snapshots.

This module does not fetch quotes, connect to MT5, or create paper/live orders.
Freshness policy must be supplied explicitly by the caller.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from typing import Any, Mapping

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_ALLOWED_FIELDS = {
    "schema_version", "snapshot_id", "source_id", "symbol", "bid", "ask",
    "event_time", "received_time",
}
MAX_CONFIGURABLE_AGE_SECONDS = 60.0
MAX_FUTURE_SKEW_SECONDS = 2.0


def _parse_aware_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or _RFC3339_RE.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _finite_positive_number(value: Any) -> float | None:
    number = _finite_number(value)
    return number if number is not None and number > 0 else None


def validate_market_snapshot(
    snapshot: Mapping[str, Any],
    *,
    max_age_seconds: float | None = None,
    future_skew_seconds: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate shape, quote sanity, clock order, and explicit freshness policy.

    Source event time and local receipt time are both checked. Callers must provide
    explicit thresholds. Configured age may not exceed 60 seconds and tolerated future
    skew may not exceed 2 seconds; deployments should normally choose stricter limits.
    Small tolerated clock skew is reported separately, and returned ages are clamped
    to zero so downstream logic never mistakes a future timestamp for negative age.
    """
    if not isinstance(snapshot, Mapping):
        return {"valid": False, "blockers": ["SNAPSHOT_INVALID"]}

    blockers: list[str] = []
    unknown_fields = set(snapshot) - _ALLOWED_FIELDS
    if unknown_fields:
        blockers.append("UNKNOWN_FIELDS")

    try:
        current = datetime.now(timezone.utc) if now is None else now
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("timezone required")
        current = current.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return {"valid": False, "blockers": ["CURRENT_TIME_INVALID"]}

    version = snapshot.get("schema_version")
    if isinstance(version, bool) or version != 1:
        blockers.append("SCHEMA_VERSION_UNSUPPORTED")
    for key, code in (("snapshot_id", "SNAPSHOT_ID_MISSING"), ("source_id", "SOURCE_ID_MISSING")):
        if not isinstance(snapshot.get(key), str) or not snapshot[key].strip():
            blockers.append(code)
    symbol = snapshot.get("symbol")
    if not isinstance(symbol, str) or _SYMBOL_RE.fullmatch(symbol) is None:
        blockers.append("SYMBOL_INVALID")

    bid = _finite_positive_number(snapshot.get("bid"))
    ask = _finite_positive_number(snapshot.get("ask"))
    if bid is None:
        blockers.append("BID_INVALID")
    if ask is None:
        blockers.append("ASK_INVALID")
    if bid is not None and ask is not None and ask < bid:
        blockers.append("CROSSED_QUOTE")

    age_limit = _finite_number(max_age_seconds)
    if age_limit is None or age_limit <= 0 or age_limit > MAX_CONFIGURABLE_AGE_SECONDS:
        blockers.append("MAX_AGE_POLICY_INVALID")
        age_limit = None
    skew_limit = _finite_number(future_skew_seconds)
    if skew_limit is None or skew_limit < 0 or skew_limit > MAX_FUTURE_SKEW_SECONDS:
        blockers.append("FUTURE_SKEW_POLICY_INVALID")
        skew_limit = None

    event_time = _parse_aware_time(snapshot.get("event_time"))
    received_time = _parse_aware_time(snapshot.get("received_time"))
    if event_time is None:
        blockers.append("EVENT_TIME_INVALID")
    if received_time is None:
        blockers.append("RECEIVED_TIME_INVALID")

    event_future_seconds = max(0.0, (event_time - current).total_seconds()) if event_time is not None else None
    received_future_seconds = max(0.0, (received_time - current).total_seconds()) if received_time is not None else None
    event_age_seconds = max(0.0, (current - event_time).total_seconds()) if event_time is not None else None
    received_age_seconds = max(0.0, (current - received_time).total_seconds()) if received_time is not None else None

    if skew_limit is not None:
        if event_future_seconds is not None and event_future_seconds > skew_limit:
            blockers.append("EVENT_TIME_IN_FUTURE")
        if received_future_seconds is not None and received_future_seconds > skew_limit:
            blockers.append("RECEIVED_TIME_IN_FUTURE")
        if event_time is not None and received_time is not None:
            if (event_time - received_time).total_seconds() > skew_limit:
                blockers.append("EVENT_AFTER_RECEIPT_TIME")

    if age_limit is not None:
        if event_age_seconds is not None and event_age_seconds > age_limit:
            blockers.append("QUOTE_STALE")
        if received_age_seconds is not None and received_age_seconds > age_limit:
            blockers.append("SNAPSHOT_STALE")

    return {
        "valid": not blockers,
        "blockers": sorted(set(blockers)),
        "spread": ask - bid if bid is not None and ask is not None and ask >= bid else None,
        "age_seconds": event_age_seconds,
        "received_age_seconds": received_age_seconds,
        "event_clock_skew_seconds": event_future_seconds,
        "received_clock_skew_seconds": received_future_seconds,
    }
