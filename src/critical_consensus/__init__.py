"""Fail-closed reference implementation for critical-consensus trade gating."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .pipeline import evaluate_signal_bundle as _evaluate_signal_bundle
from .strict import chief_skeptic_pass

__version__ = "0.2.0"


def evaluate_signal_bundle(bundle: Mapping[str, Any], certainty_policy: Mapping[str, Any],
                           permission_matrix: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Public fail-closed wrapper; malformed inputs never escape as exceptions."""
    try:
        return _evaluate_signal_bundle(bundle, certainty_policy, permission_matrix, now=now)
    except (TypeError, ValueError, KeyError, OverflowError, AttributeError):
        return {"status": "GATHER_MORE", "trade_allowed": False,
                "blockers": ["INVALID_SIGNAL_BUNDLE"], "audit_events": [], "details": {}}


__all__ = ["__version__", "chief_skeptic_pass", "evaluate_signal_bundle"]
