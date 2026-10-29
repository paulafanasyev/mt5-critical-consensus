"""Fail-closed reference implementation for critical-consensus trade gating."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .contracts import validate_contract
from . import gates as _gates

__version__ = "0.2.0"

_unchecked_chief_skeptic_pass = _gates.chief_skeptic_pass


def chief_skeptic_pass(reasoning: Mapping[str, Any], *, max_tail_risk_probability: float = 0.05,
                       now: datetime | None = None) -> dict[str, Any]:
    """Public strict Chief gate: contract validity and freshness are mandatory."""
    result = _unchecked_chief_skeptic_pass(reasoning, max_tail_risk_probability=max_tail_risk_probability, now=now)
    blockers = list(result.get("blockers", []))
    valid, _ = validate_contract("chief_reasoning.schema.json", reasoning)
    if not valid:
        blockers.append("CHIEF_REASONING_SCHEMA_INVALID")
    try:
        current = datetime.now(timezone.utc) if now is None else now
        if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("timezone-aware now required")
        current = current.astimezone(timezone.utc)
        created_raw = reasoning.get("created_at") if isinstance(reasoning, Mapping) else None
        if not isinstance(created_raw, str):
            raise ValueError("timestamp must be a string")
        created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        if created.tzinfo is None or created.utcoffset() is None:
            raise ValueError("timezone required")
        age = (current - created.astimezone(timezone.utc)).total_seconds()
        if age > 300:
            blockers.append("CHIEF_REASONING_EXPIRED")
        elif age < -30:
            blockers.append("CHIEF_CREATED_AT_IN_FUTURE")
    except (ValueError, TypeError, OverflowError, AttributeError):
        blockers.append("CHIEF_CREATED_AT_INVALID")
    if blockers:
        result["verdict"] = "REJECT" if result.get("verdict") == "REJECT" else "HOLD"
        result["passed"] = False
        result["blockers"] = sorted(set(blockers))
    return result


_gates.chief_skeptic_pass = chief_skeptic_pass

from . import pipeline as _pipeline  # noqa: E402

_unchecked_evaluate_signal_bundle = _pipeline.evaluate_signal_bundle


def evaluate_signal_bundle(bundle: Mapping[str, Any], certainty_policy: Mapping[str, Any],
                           permission_matrix: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Public pipeline boundary; malformed inputs must never escape as an exception."""
    try:
        return _unchecked_evaluate_signal_bundle(bundle, certainty_policy, permission_matrix, now=now)
    except (TypeError, ValueError, KeyError, OverflowError, AttributeError):
        return {"status": "GATHER_MORE", "trade_allowed": False,
                "blockers": ["INVALID_SIGNAL_BUNDLE"], "audit_events": [], "details": {}}


_pipeline.evaluate_signal_bundle = evaluate_signal_bundle
__all__ = ["__version__", "chief_skeptic_pass", "evaluate_signal_bundle"]
