"""Strict public wrappers around the low-level policy primitives."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .contracts import validate_contract
from .gates import chief_skeptic_pass as _chief_skeptic_pass


def chief_skeptic_pass(reasoning: Mapping[str, Any], *, max_tail_risk_probability: float = 0.05,
                       now: datetime | None = None) -> dict[str, Any]:
    """Validate Chief schema, freshness and policy before returning a verdict."""
    try:
        result = _chief_skeptic_pass(reasoning, max_tail_risk_probability=max_tail_risk_probability, now=now)
    except (TypeError, ValueError, KeyError, OverflowError, AttributeError):
        return {"verdict": "HOLD", "passed": False, "trade_allowed": False,
                "blockers": ["CHIEF_REASONING_INVALID"]}
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
