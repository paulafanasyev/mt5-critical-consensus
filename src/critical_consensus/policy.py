"""Fail-closed policy helpers for consensus decisions."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class Decision(str, Enum):
    APPROVE = "APPROVE"
    GATHER_MORE = "GATHER_MORE"
    ESCALATE = "ESCALATE"
    REJECT = "REJECT"


@dataclass(frozen=True)
class GateResult:
    decision: Decision
    reasons: tuple[str, ...]


def evaluate_consensus(
    *,
    score: float,
    threshold: float = 0.98,
    independent_reviewers: int = 0,
    required_reviewers: int = 2,
    unresolved_contradictions: Iterable[str] = (),
    compliance_clear: bool = False,
    risk_clear: bool = False,
) -> GateResult:
    """Approve only if every mandatory gate passes; otherwise fail closed."""
    reasons: list[str] = []
    if not 0.0 <= score <= 1.0:
        reasons.append("consensus score must be between 0 and 1")
    if score < threshold:
        reasons.append("consensus threshold not met")
    if independent_reviewers < required_reviewers:
        reasons.append("insufficient independent reviewers")
    if tuple(unresolved_contradictions):
        reasons.append("unresolved contradictions remain")
    if not compliance_clear:
        reasons.append("compliance gate not cleared")
    if not risk_clear:
        reasons.append("risk gate not cleared")
    if reasons:
        return GateResult(Decision.GATHER_MORE, tuple(reasons))
    return GateResult(Decision.APPROVE, ())
