"""Fail-closed validation for normalized, read-only market snapshots.

This module does not fetch quotes, connect to MT5, or create paper/live orders.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from typing import Any, Mapping

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def _parse_aware_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _finite_positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def validate_market_snapshot(
    snapshot: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_age_seconds: float = 5.0,
    future_skew_seconds: float = 2.0,
) -> dict[str, Any]:
    """Return a validation result; any missing, malformed, stale, or crossed quote fails closed.

    Freshness is checked for both the source event time and local receipt time. Thresholds
    are caller-supplied policy values and must be explicitly configured by the deployment.
    """
    if not isinstance(snapshot, Mapping):
        return {"valid": False, "blockers": ["SNAPSHOT_INVALID"]}

    blockers: list[str] = []
    try:
        current = datetime.now(timezone.utc) if now is None else now
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("timezone required")
        current = current.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return {"valid": False, "blockers": ["CURRENT_TIME_INVALID"]}

    if snapshot.get("schema_version") != 1 or isinstance(snapshot.get("schema_version"), bool):
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

    age_limit = max_age_seconds
    skew_limit = future_skew_seconds
    if (isinstance(age_limit, bool) or not isinstance(age_limit, (int, float))
            or not math.isfinite(float(age_limit)) or age_limit <= 0):
        blockers.append("MAX_AGE_POLICY_INVALID")
        age_limit = None
    if (isinstance(skew_limit, bool) or not isinstance(skew_limit, (int, float))
            or not math.isfinite(float(skew_limit)) or skew_limit < 0):
        blockers.append("FUTURE_SKEW_POLICY_INVALID")
        skew_limit = None

    event_time = _parse_aware_time(snapshot.get("event_time"))
    received_time = _parse_aware_time(snapshot.get("received_time"))
    if event_time is None:
        blockers.append("EVENT_TIME_INVALID")
    if received_time is None:
        blockers.append("RECEIVED_TIME_INVALID")

    if skew_limit is not None:
        if event_time is not None and (event_time - current).total_seconds() > skew_limit:
            blockers.append("EVENT_TIME_IN_FUTURE")
        if received_time is not None and (received_time - current).total_seconds() > skew_limit:
            blockers.append("RECEIVED_TIME_IN_FUTURE")
        if event_time is not None and received_time is not None:
            if (event_time - received_time).total_seconds() > skew_limit:
                blockers.append("EVENT_AFTER_RECEIPT_TIME")

    if age_limit is not None:
        if event_time is not None and (current - event_time).total_seconds() > age_limit:
            blockers.append("QUOTE_STALE")
        if received_time is not None and (current - received_time).total_seconds() > age_limit:
            blockers.append("SNAPSHOT_STALE")

    return {
        "valid": not blockers,
        "blockers": sorted(set(blockers)),
        "spread": ask - bid if bid is not None and ask is not None and ask >= bid else None,
        "age_seconds": (current - event_time).total_seconds() if event_time is not None else None,
    }
