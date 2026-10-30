"""Execution-adjacent risk controls; all unknown or stale state blocks by default."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping

from .gates import permission_gate


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _stamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def global_kill_switch_gate(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Only an explicit persisted inactive switch opens this gate; missing state blocks."""
    if not isinstance(state, Mapping):
        return {"status": "DENY", "trade_allowed": False, "blockers": ["KILL_SWITCH_STATE_UNKNOWN"]}
    active = state.get("kill_switch_active")
    if active is not False:
        return {"status": "DENY", "trade_allowed": False, "blockers": ["KILL_SWITCH_ACTIVE_OR_INVALID"]}
    if not str(state.get("updated_by", "")).strip() or not str(state.get("reason", "")).strip():
        return {"status": "DENY", "trade_allowed": False, "blockers": ["KILL_SWITCH_AUDIT_METADATA_MISSING"]}
    updated = _stamp(state.get("updated_at"))
    if updated is None:
        return {"status": "DENY", "trade_allowed": False, "blockers": ["KILL_SWITCH_TIMESTAMP_INVALID"]}
    return {"status": "PASS", "trade_allowed": False, "blockers": []}


def risk_snapshot_gate(
    snapshot: Mapping[str, Any] | None, *, now: datetime | None = None,
    max_age_seconds: int = 15,
) -> dict[str, Any]:
    """Validate freshness and coverage of a read-only MT5 account risk snapshot."""
    if not isinstance(snapshot, Mapping):
        return {"status": "DENY", "trade_allowed": False, "blockers": ["RISK_SNAPSHOT_MISSING"]}
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        return {"status": "DENY", "trade_allowed": False, "blockers": ["CURRENT_TIME_INVALID"]}
    current = current.astimezone(timezone.utc)
    stamp = _stamp(snapshot.get("observed_at"))
    blockers: list[str] = []
    if stamp is None:
        blockers.append("RISK_SNAPSHOT_TIMESTAMP_INVALID")
    else:
        age = (current - stamp).total_seconds()
        if age < -30:
            blockers.append("RISK_SNAPSHOT_FROM_FUTURE")
        elif age > max_age_seconds:
            blockers.append("RISK_SNAPSHOT_STALE")
    if snapshot.get("status") != "PASS":
        blockers.append("MT5_RISK_COLLECTION_NOT_PASS")
    if snapshot.get("account_mode") != "demo":
        blockers.append("MT5_ACCOUNT_NOT_DEMO_OR_UNKNOWN")
    if not str(snapshot.get("symbol", "")).strip():
        blockers.append("RISK_SNAPSHOT_SYMBOL_MISSING")
    required_positive = ("account_equity", "day_start_equity", "equity_high_watermark")
    values = {key: _finite(snapshot.get(key)) for key in required_positive}
    for key, value in values.items():
        if value is None or value <= 0:
            blockers.append(f"RISK_SNAPSHOT_{key.upper()}_MISSING_OR_INVALID")
    risk_fraction = _finite(snapshot.get("risk_fraction"))
    daily_loss = _finite(snapshot.get("daily_loss_fraction"))
    drawdown = _finite(snapshot.get("drawdown_fraction"))
    if risk_fraction is None or not 0 <= risk_fraction <= 1:
        blockers.append("RISK_FRACTION_MISSING_OR_INVALID")
    if daily_loss is None or not 0 <= daily_loss <= 1:
        blockers.append("DAILY_LOSS_MISSING_OR_INVALID")
    if drawdown is None or not 0 <= drawdown <= 1:
        blockers.append("DRAWDOWN_MISSING_OR_INVALID")
    for key in ("open_positions", "correlated_open_positions"):
        count = _finite(snapshot.get(key))
        if count is None or count < 0 or not count.is_integer():
            blockers.append(f"{key.upper()}_MISSING_OR_INVALID")
    if snapshot.get("symbol_group") is None:
        blockers.append("SYMBOL_CORRELATION_GROUP_UNKNOWN")
    if blockers:
        return {"status": "DENY", "trade_allowed": False, "blockers": sorted(set(blockers))}
    return {"status": "PASS", "trade_allowed": False, "blockers": [], "observed_at": stamp.isoformat()}


def risk_controlled_permission(
    request: Mapping[str, Any], matrix: Mapping[str, Any], *,
    kill_switch_state: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None,
    now: datetime | None = None,
    snapshot_max_age_seconds: int = 15,
) -> dict[str, Any]:
    """Require kill-switch and fresh terminal risk gates before the policy permission gate."""
    switch = global_kill_switch_gate(kill_switch_state)
    risk = risk_snapshot_gate(snapshot, now=now, max_age_seconds=snapshot_max_age_seconds)
    if switch["status"] != "PASS" or risk["status"] != "PASS":
        blockers = list(switch.get("blockers", [])) + list(risk.get("blockers", []))
        return {"status": "DENY", "allowed": False, "trade_allowed": False,
                "blockers": sorted(set(blockers)), "kill_switch": switch, "risk_snapshot": risk}
    merged = dict(request)
    merged["risk_snapshot"] = {
        **dict(merged.get("risk_snapshot", {}) if isinstance(merged.get("risk_snapshot"), Mapping) else {}),
        "risk_fraction": snapshot["risk_fraction"],
        "daily_loss_fraction": snapshot["daily_loss_fraction"],
        "drawdown_fraction": snapshot["drawdown_fraction"],
        "open_positions": snapshot["open_positions"],
        "correlated_open_positions": snapshot["correlated_open_positions"],
    }
    if snapshot.get("within_high_impact_news_window") is not False:
        merged["within_high_impact_news_window"] = None
    result = permission_gate(merged, matrix, now=now)
    result["kill_switch"] = switch
    result["risk_snapshot_validation"] = risk
    result["trade_allowed"] = False
    return result
