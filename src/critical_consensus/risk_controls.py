"""Execution-adjacent risk controls; all unknown or stale state blocks by default."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Mapping

from .gates import permission_gate

# A fill further than this share of the stop distance from the reviewed entry means the
# order no longer resembles what the human approved; sizing must not absorb the drift.
_MAX_SLIPPAGE_FRACTION_OF_STOP = 0.10
# Starting equity plus realised P/L must reconstruct the balance to within this share.
_BASELINE_DRIFT_TOLERANCE = 0.01


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


def _adjusted_rr(request: Mapping[str, Any], snapshot: Mapping[str, Any]) -> float | None:
    """Reward/risk at the worst plausible fill, matching how the snapshot sizes risk."""
    direction = str(request.get("direction", "")).strip().upper()
    if direction not in {"BUY", "SELL"}:
        return None
    sl_tp = request.get("sl_tp")
    if not isinstance(sl_tp, Mapping):
        return None
    entry = _finite(request.get("expected_price"))
    stop = _finite(sl_tp.get("sl"))
    take = _finite(sl_tp.get("tp"))
    reference = _finite(snapshot.get("reference_price"))
    if entry is None or stop is None or take is None or reference is None:
        return None
    conservative_entry = max(entry, reference) if direction == "BUY" else min(entry, reference)
    risk_distance = abs(conservative_entry - stop)
    reward_distance = abs(take - conservative_entry)
    if risk_distance <= 0 or reward_distance <= 0:
        return None
    return reward_distance / risk_distance


def global_kill_switch_gate(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Only an explicit persisted inactive switch opens this gate; missing state blocks."""
    if not isinstance(state, Mapping):
        return {"status": "DENY", "trade_allowed": False, "blockers": ["KILL_SWITCH_STATE_UNKNOWN"]}
    active = state.get("kill_switch_active")
    if active is not False:
        return {"status": "DENY", "trade_allowed": False, "blockers": ["KILL_SWITCH_ACTIVE_OR_INVALID"]}
    if state.get("status") != "PERSISTED":
        return {"status": "DENY", "trade_allowed": False, "blockers": ["KILL_SWITCH_STATE_NOT_PERSISTED"]}
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
        # A small clock skew is tolerable, but a timestamp further in the future than the
        # freshness budget must not extend how long a stale snapshot is accepted.
        if age < -min(5.0, float(max_age_seconds)):
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
    if (values["equity_high_watermark"] is not None and values["account_equity"] is not None
            and values["equity_high_watermark"] < values["account_equity"]):
        # A peak below current equity would silently zero out the drawdown calculation.
        blockers.append("RISK_SNAPSHOT_EQUITY_HIGH_WATERMARK_BELOW_EQUITY")
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
    if not str(snapshot.get("symbol_group") or "").strip():
        blockers.append("SYMBOL_CORRELATION_GROUP_UNKNOWN")
    balance = _finite(snapshot.get("account_balance"))
    if balance is None or balance <= 0:
        blockers.append("RISK_SNAPSHOT_ACCOUNT_BALANCE_MISSING_OR_INVALID")
    daily_pnl = _finite(snapshot.get("daily_pnl"))
    if daily_pnl is None:
        blockers.append("RISK_SNAPSHOT_DAILY_PNL_MISSING_OR_INVALID")
    if (values["equity_high_watermark"] is not None and values["day_start_equity"] is not None
            and values["equity_high_watermark"] < values["day_start_equity"]):
        # The intraday peak cannot sit below the equity the session started at; if it does,
        # the drawdown buffer is fabricated and would mask a loss.
        blockers.append("RISK_SNAPSHOT_EQUITY_HIGH_WATERMARK_BELOW_DAY_START")
    if (balance is not None and balance > 0 and daily_pnl is not None
            and values["day_start_equity"] is not None
            and values["day_start_equity"] + daily_pnl
            < balance * (1 - _BASELINE_DRIFT_TOLERANCE)):
        # Starting equity plus realised P/L must reconstruct the current balance. An
        # understated baseline loosens the daily-loss limit; cashflows must be adjusted
        # explicitly by the operator instead of being silently absorbed here.
        blockers.append("DAY_START_EQUITY_BELOW_DAILY_PNL_IMPLIED_BALANCE")
    binding = snapshot.get("request_binding_sha256")
    if not isinstance(binding, str) or len(binding) != 71 or not binding.startswith("sha256:") or any(ch not in "0123456789abcdef" for ch in binding[7:]):
        blockers.append("RISK_REQUEST_BINDING_MISSING_OR_INVALID")
    if blockers:
        return {"status": "DENY", "trade_allowed": False, "blockers": sorted(set(blockers))}
    return {"status": "PASS", "trade_allowed": False, "blockers": [], "observed_at": stamp.isoformat()}


def risk_snapshot_matches_order(
    request: Mapping[str, Any], snapshot: Mapping[str, Any], *,
    tolerance_fraction: float = 0.01,
) -> list[str]:
    """Recompute the snapshot's risk metrics from the exact order it claims to measure.

    A snapshot is persisted by a separate process, so its aggregate percentages must
    agree with its own equity and tick specification; otherwise a tampered or
    mismatched row could report an artificially small ``risk_fraction``.
    """
    tolerance = _finite(tolerance_fraction)
    if tolerance is None or not 0 <= tolerance <= 0.5:
        return ["RISK_RECONCILIATION_POLICY_INVALID"]
    blockers: list[str] = []
    instrument = str(request.get("instrument", "")).strip().upper()
    if str(snapshot.get("symbol", "")).strip().upper() != instrument:
        blockers.append("RISK_SNAPSHOT_SYMBOL_MISMATCH")
    sl_tp = request.get("sl_tp")
    direction = str(request.get("direction", "")).strip().upper()
    size = _finite(request.get("size"))
    entry = _finite(request.get("expected_price"))
    stop = _finite(sl_tp.get("sl")) if isinstance(sl_tp, Mapping) else None
    take = _finite(sl_tp.get("tp")) if isinstance(sl_tp, Mapping) else None
    equity = _finite(snapshot.get("account_equity"))
    tick_size = _finite(snapshot.get("risk_tick_size"))
    tick_value_loss = _finite(snapshot.get("risk_tick_value_loss"))
    reference = _finite(snapshot.get("reference_price"))
    if (size is None or entry is None or stop is None or take is None
            or direction not in {"BUY", "SELL"}):
        return sorted(set(blockers + ["RISK_REQUEST_FIELDS_INVALID"]))
    if equity is None or equity <= 0:
        return sorted(set(blockers + ["RISK_SNAPSHOT_ACCOUNT_EQUITY_MISSING_OR_INVALID"]))
    if tick_size is None or tick_size <= 0 or tick_value_loss is None or tick_value_loss <= 0:
        return sorted(set(blockers + ["RISK_SNAPSHOT_TICK_SPEC_MISSING_OR_INVALID"]))
    if reference is None or reference <= 0:
        return sorted(set(blockers + ["RISK_SNAPSHOT_REFERENCE_PRICE_MISSING_OR_INVALID"]))
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return sorted(set(blockers + ["RISK_REQUEST_FIELDS_INVALID"]))
    if abs(reference - entry) > stop_distance * _MAX_SLIPPAGE_FRACTION_OF_STOP:
        blockers.append("RISK_SLIPPAGE_EXCEEDS_STOP_DISTANCE_FRACTION")

    # Size the worst plausible fill, not the reviewed entry: a stop is only as wide as the
    # price the order actually gets. Buying can fill at the higher reference, selling lower.
    conservative_entry = max(entry, reference) if direction == "BUY" else min(entry, reference)
    geometry_ok = (stop < conservative_entry < take) if direction == "BUY" else (
        take < conservative_entry < stop)
    if not geometry_ok:
        blockers.append("RISK_CONSERVATIVE_ENTRY_OUTSIDE_GEOMETRY")

    expected_value = abs(conservative_entry - stop) / tick_size * tick_value_loss * size
    expected_fraction = expected_value / equity
    reported_value = _finite(snapshot.get("risk_value_account_currency"))
    reported_fraction = _finite(snapshot.get("risk_fraction"))

    def disagrees(reported: float | None, expected: float, name: str) -> None:
        if reported is None or abs(reported - expected) > max(1e-12, expected * tolerance):
            blockers.append(name)

    disagrees(reported_fraction, expected_fraction, "RISK_FRACTION_DOES_NOT_MATCH_ORDER")
    if reported_value is None:
        blockers.append("RISK_VALUE_MISSING_OR_INVALID")
    else:
        disagrees(reported_value, expected_value, "RISK_VALUE_DOES_NOT_MATCH_ORDER")

    day_start = _finite(snapshot.get("day_start_equity"))
    peak = _finite(snapshot.get("equity_high_watermark"))
    if day_start is not None and day_start > 0:
        disagrees(_finite(snapshot.get("daily_loss_fraction")),
                  max(0.0, (day_start - equity) / day_start),
                  "DAILY_LOSS_DOES_NOT_MATCH_EQUITY")
    if peak is not None and peak > 0:
        disagrees(_finite(snapshot.get("drawdown_fraction")),
                  max(0.0, (peak - equity) / peak),
                  "DRAWDOWN_DOES_NOT_MATCH_EQUITY")
    return sorted(set(blockers))


def risk_controlled_permission(
    request: Mapping[str, Any], matrix: Mapping[str, Any], *,
    kill_switch_state: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None,
    now: datetime | None = None,
    snapshot_max_age_seconds: int = 15,
    risk_fraction_tolerance_fraction: float = 0.01,
) -> dict[str, Any]:
    """Require kill-switch and fresh terminal risk gates before the policy permission gate."""
    switch = global_kill_switch_gate(kill_switch_state)
    risk = risk_snapshot_gate(snapshot, now=now, max_age_seconds=snapshot_max_age_seconds)
    if switch["status"] != "PASS" or risk["status"] != "PASS" or not isinstance(snapshot, Mapping):
        blockers = list(switch.get("blockers", [])) + list(risk.get("blockers", []))
        return {"status": "DENY", "allowed": False, "trade_allowed": False,
                "blockers": sorted(set(blockers)), "kill_switch": switch, "risk_snapshot": risk}
    try:
        expected_binding = risk_request_binding_hash(request)
    except ValueError:
        return {"status": "DENY", "allowed": False, "trade_allowed": False,
                "blockers": ["RISK_REQUEST_BINDING_INVALID"], "kill_switch": switch, "risk_snapshot": risk}
    if snapshot.get("request_binding_sha256") != expected_binding:
        return {"status": "DENY", "allowed": False, "trade_allowed": False,
                "blockers": ["RISK_REQUEST_BINDING_MISMATCH"], "kill_switch": switch, "risk_snapshot": risk}
    reconciliation = risk_snapshot_matches_order(
        request, snapshot, tolerance_fraction=risk_fraction_tolerance_fraction,
    )
    if reconciliation:
        return {"status": "DENY", "allowed": False, "trade_allowed": False,
                "blockers": reconciliation, "kill_switch": switch, "risk_snapshot": risk,
                "risk_reconciliation": {"status": "FAILED", "blockers": reconciliation}}
    policy = matrix.get("permission_matrix") if isinstance(matrix, Mapping) else None
    if isinstance(policy, Mapping):
        # The policy gate recomputes RR at the reviewed entry; a worse fill can leave that
        # same order below the reward floor, so the conservative RR must clear it too.
        min_rr = _finite(policy.get("min_expected_rr", 1.5))
        adjusted_rr = _adjusted_rr(request, snapshot)
        if min_rr is not None and min_rr >= 0 and (adjusted_rr is None or adjusted_rr + 1e-9 < min_rr):
            return {"status": "DENY", "allowed": False, "trade_allowed": False,
                    "blockers": ["RISK_ADJUSTED_RR_BELOW_MINIMUM"], "kill_switch": switch,
                    "risk_snapshot": risk, "adjusted_expected_rr": adjusted_rr,
                    "risk_reconciliation": {"status": "PASS", "blockers": []}}
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
    result["risk_reconciliation"] = {"status": "PASS", "blockers": []}
    result["trade_allowed"] = False
    return result
