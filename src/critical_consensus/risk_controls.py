"""Execution-adjacent risk controls; all unknown or stale state blocks by default."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Mapping

from .gates import permission_gate


MAX_RISK_SNAPSHOT_QUOTE_AGE_SECONDS = 15


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


def risk_request_binding_hash(request: Mapping[str, Any]) -> str:
    """Hash the exact proposed order fields measured by the terminal risk collector."""
    if not isinstance(request, Mapping):
        raise ValueError("request must be an object")
    instrument = str(request.get("instrument", "")).strip().upper()
    direction = str(request.get("direction", "")).strip().upper()
    sl_tp = request.get("sl_tp")
    if not isinstance(sl_tp, Mapping):
        raise ValueError("sl_tp must be an object")
    values = {}
    for key, value in (
        ("size", request.get("size")),
        ("expected_price", request.get("expected_price")),
        ("sl", sl_tp.get("sl")),
        ("tp", sl_tp.get("tp")),
    ):
        number = _finite(value)
        if number is None or number <= 0:
            raise ValueError(f"{key} must be a positive finite number")
        values[key] = number
    if not instrument or direction not in {"BUY", "SELL"}:
        raise ValueError("instrument and BUY/SELL direction are required")
    if direction == "BUY" and not values["sl"] < values["expected_price"] < values["tp"]:
        raise ValueError("BUY request has invalid SL/entry/TP geometry")
    if direction == "SELL" and not values["tp"] < values["expected_price"] < values["sl"]:
        raise ValueError("SELL request has invalid SL/entry/TP geometry")
    payload = {"instrument": instrument, "direction": direction, **values}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
    if (isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int)
            or not 1 <= max_age_seconds <= 300):
        return {"status": "DENY", "trade_allowed": False,
                "blockers": ["RISK_SNAPSHOT_MAX_AGE_POLICY_INVALID"]}
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
    raw_quote_age = snapshot.get("quote_age_seconds")
    quote_age = (
        _finite(raw_quote_age)
        if not isinstance(raw_quote_age, bool) and isinstance(raw_quote_age, (int, float))
        else None
    )
    raw_quote_age_policy = snapshot.get("max_quote_age_seconds")
    quote_age_policy = (
        raw_quote_age_policy
        if not isinstance(raw_quote_age_policy, bool) and isinstance(raw_quote_age_policy, int)
        else None
    )
    if (quote_age_policy is None
            or not 1 <= quote_age_policy <= MAX_RISK_SNAPSHOT_QUOTE_AGE_SECONDS):
        blockers.append("MT5_QUOTE_AGE_POLICY_INVALID")
    if quote_age is None or quote_age < -5:
        blockers.append("MT5_QUOTE_AGE_INVALID_OR_FUTURE")
    elif quote_age_policy is not None and quote_age > min(
            float(MAX_RISK_SNAPSHOT_QUOTE_AGE_SECONDS), float(quote_age_policy)):
        blockers.append("MT5_QUOTE_STALE")
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
    spread_points = _finite(snapshot.get("spread_points"))
    max_spread_points = _finite(snapshot.get("max_spread_points"))
    if spread_points is None or spread_points < 0:
        blockers.append("SPREAD_POINTS_MISSING_OR_INVALID")
    if max_spread_points is None or max_spread_points < 0:
        blockers.append("MAX_SPREAD_POLICY_MISSING_OR_INVALID")
    elif spread_points is not None and spread_points > max_spread_points:
        blockers.append("RISK_SNAPSHOT_SPREAD_LIMIT_EXCEEDED")
    position_counts: dict[str, float | None] = {}
    for key in ("open_positions", "correlated_open_positions"):
        count = _finite(snapshot.get(key))
        position_counts[key] = count
        if count is None or count < 0 or not count.is_integer():
            blockers.append(f"{key.upper()}_MISSING_OR_INVALID")
    if (position_counts["open_positions"] is not None
            and position_counts["correlated_open_positions"] is not None
            and position_counts["correlated_open_positions"] > position_counts["open_positions"]):
        blockers.append("CORRELATED_POSITION_COUNT_EXCEEDS_OPEN_POSITIONS")
    if not str(snapshot.get("symbol_group", "")).strip():
        blockers.append("SYMBOL_CORRELATION_GROUP_UNKNOWN")
    binding = snapshot.get("request_binding_sha256")
    if not isinstance(binding, str) or len(binding) != 71 or not binding.startswith("sha256:") or any(ch not in "0123456789abcdef" for ch in binding[7:]):
        blockers.append("RISK_REQUEST_BINDING_MISSING_OR_INVALID")
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
    request_symbol = str(request.get("instrument", "")).strip().upper()
    snapshot_symbol = str(snapshot.get("symbol", "")).strip().upper()
    if not request_symbol or snapshot_symbol != request_symbol:
        return {"status": "DENY", "allowed": False, "trade_allowed": False,
                "blockers": ["RISK_SNAPSHOT_SYMBOL_MISMATCH"], "kill_switch": switch,
                "risk_snapshot": risk}
    try:
        expected_binding = risk_request_binding_hash(request)
    except ValueError:
        return {"status": "DENY", "allowed": False, "trade_allowed": False,
                "blockers": ["RISK_REQUEST_BINDING_INVALID"], "kill_switch": switch, "risk_snapshot": risk}
    if snapshot.get("request_binding_sha256") != expected_binding:
        return {"status": "DENY", "allowed": False, "trade_allowed": False,
                "blockers": ["RISK_REQUEST_BINDING_MISMATCH"], "kill_switch": switch, "risk_snapshot": risk}
    merged = dict(request)
    merged["risk_snapshot"] = {
        **dict(merged.get("risk_snapshot", {}) if isinstance(merged.get("risk_snapshot"), Mapping) else {}),
        "risk_fraction": snapshot["risk_fraction"],
        "daily_loss_fraction": snapshot["daily_loss_fraction"],
        "drawdown_fraction": snapshot["drawdown_fraction"],
        "open_positions": snapshot["open_positions"],
        "correlated_open_positions": snapshot["correlated_open_positions"],
        "spread_points": snapshot["spread_points"],
    }
    if snapshot.get("within_high_impact_news_window") is not False:
        merged["within_high_impact_news_window"] = None
    result = permission_gate(merged, matrix, now=now)
    result["kill_switch"] = switch
    result["risk_snapshot_validation"] = risk
    result["trade_allowed"] = False
    return result
