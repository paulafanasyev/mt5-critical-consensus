"""Governance metric attestations produced by the calibration service.

``dissent_rate`` and ``false_certainty_rate`` need a historical, outcome-verified
window; they cannot be inferred from the current signal bundle. The calibration
service therefore signs an attestation with a key that is distinct from the ingress
key. The pipeline verifies provenance, freshness, window geometry and value types
before certainity thresholds consume these numbers. This module does not recompute
the metrics from the persistent store; the deployment adapter must do that upstream.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Mapping, Sequence

from .evidence import EvidenceError, make_envelope, verify_wrapper

DEFAULT_MAX_AGE_SECONDS = 86_400
MAX_WINDOW_DAYS = 31.0
SIGNING_DOMAIN = "governance"
_REQUIRED_FIELDS = {
    "source", "observed_at", "window_start", "window_end",
    "dissent_rate", "dissent_sample_size",
    "false_certainty_rate", "false_certainty_sample_size",
    "method_version", "agent_calibration",
}


def _utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvidenceError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _rate(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceError(f"{field} must be a finite number in [0,1]")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise EvidenceError(f"{field} must be a finite number in [0,1]")
    return number


def _count(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvidenceError(f"{field} must be a non-negative integer")
    return value


def _agent_calibration(value: Any) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Validate the per-agent calibration report carried inside the attestation."""
    if not isinstance(value, Mapping) or not value:
        return {}, ["GOVERNANCE_AGENT_CALIBRATION_MISSING_OR_INVALID"]
    validated: dict[str, dict[str, Any]] = {}
    blockers: list[str] = []
    for raw_id, row in value.items():
        agent_id = str(raw_id).strip()
        if not agent_id or len(agent_id) > 128:
            blockers.append("GOVERNANCE_AGENT_CALIBRATION_AGENT_ID_INVALID")
            continue
        if not isinstance(row, Mapping):
            blockers.append(f"GOVERNANCE_AGENT_CALIBRATION_INVALID:{agent_id}")
            continue
        try:
            brier = _rate(row.get("brier_score"), "brier_score")
            sample = _count(row.get("sample_size"), "sample_size")
        except EvidenceError:
            blockers.append(f"GOVERNANCE_AGENT_CALIBRATION_INVALID:{agent_id}")
            continue
        status = row.get("calibration_status")
        if not isinstance(status, str) or status.strip().upper() != "CALIBRATED":
            blockers.append(f"GOVERNANCE_AGENT_CALIBRATION_NOT_CALIBRATED:{agent_id}")
            continue
        validated[agent_id] = {
            "brier_score": brier,
            "sample_size": sample,
            "calibration_status": "CALIBRATED",
        }
    if not validated and not blockers:
        blockers.append("GOVERNANCE_AGENT_CALIBRATION_MISSING_OR_INVALID")
    return validated, blockers


def validate_governance_payload(
    payload: Mapping[str, Any], *, now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    future_skew_seconds: int = 30,
) -> dict[str, Any]:
    """Validate the attestation body; every malformed value is a blocking code."""
    blockers: list[str] = []
    if not isinstance(payload, Mapping):
        return {"valid": False, "blockers": ["GOVERNANCE_PAYLOAD_INVALID"]}
    missing = sorted(_REQUIRED_FIELDS - set(payload))
    if missing:
        return {"valid": False, "blockers": ["GOVERNANCE_FIELDS_MISSING:" + ",".join(missing)]}

    source = payload.get("source")
    if not isinstance(source, str) or not source.strip() or len(source) > 128:
        blockers.append("GOVERNANCE_SOURCE_INVALID")
    method_version = payload.get("method_version")
    if not isinstance(method_version, str) or not method_version.strip() or len(method_version) > 128:
        blockers.append("GOVERNANCE_METHOD_VERSION_INVALID")

    try:
        current = datetime.now(timezone.utc) if now is None else now
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("timezone required")
        current = current.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return {"valid": False, "blockers": ["GOVERNANCE_CURRENT_TIME_INVALID"]}

    observed_at = window_start = window_end = None
    try:
        observed_at = _utc(payload.get("observed_at"), "observed_at")
    except EvidenceError:
        blockers.append("GOVERNANCE_OBSERVED_AT_INVALID")
    try:
        window_start = _utc(payload.get("window_start"), "window_start")
        window_end = _utc(payload.get("window_end"), "window_end")
    except EvidenceError:
        blockers.append("GOVERNANCE_WINDOW_INVALID")
    if observed_at is not None:
        age = (current - observed_at).total_seconds()
        if age < -int(future_skew_seconds):
            blockers.append("GOVERNANCE_OBSERVED_AT_IN_FUTURE")
        elif age > int(max_age_seconds):
            blockers.append("GOVERNANCE_ATTESTATION_STALE")
    if window_start is not None and window_end is not None:
        if window_start >= window_end:
            blockers.append("GOVERNANCE_WINDOW_INVALID")
        elif (window_end - window_start).total_seconds() > MAX_WINDOW_DAYS * 86_400:
            blockers.append("GOVERNANCE_WINDOW_TOO_LONG")
        elif observed_at is not None and window_end > observed_at:
            blockers.append("GOVERNANCE_WINDOW_ENDS_AFTER_OBSERVATION")

    rates: dict[str, float | None] = {}
    for field in ("dissent_rate", "false_certainty_rate"):
        try:
            rates[field] = _rate(payload.get(field), field)
        except EvidenceError:
            rates[field] = None
            blockers.append(f"GOVERNANCE_{field.upper()}_INVALID")
    counts: dict[str, int | None] = {}
    for field in ("dissent_sample_size", "false_certainty_sample_size"):
        try:
            counts[field] = _count(payload.get(field), field)
        except EvidenceError:
            counts[field] = None
            blockers.append(f"GOVERNANCE_{field.upper()}_INVALID")

    agent_calibration, calibration_blockers = _agent_calibration(payload.get("agent_calibration"))
    blockers.extend(calibration_blockers)

    result = {
        "valid": not blockers,
        "blockers": sorted(set(blockers)),
        "payload": dict(payload),
        "source": source if isinstance(source, str) else None,
        "observed_at": observed_at.isoformat() if observed_at else None,
        "window_start": window_start.isoformat() if window_start else None,
        "window_end": window_end.isoformat() if window_end else None,
        "method_version": method_version if isinstance(method_version, str) else None,
        "agent_calibration": agent_calibration,
    }
    result.update({field: value for field, value in rates.items()})
    result.update({field: value for field, value in counts.items()})
    return result


def verify_governance_attestation(
    wrapper: Mapping[str, Any] | None,
    *,
    key: str | bytes | bytearray | None,
    expected_issuer: str = "calibration-service",
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    future_skew_seconds: int = 30,
) -> dict[str, Any]:
    """Verify a signed ``{"payload", "envelope"}`` attestation and its metric values."""
    envelope_check = verify_wrapper(
        wrapper, key=key, expected_issuer=expected_issuer, now=now,
        max_age_seconds=max_age_seconds, future_skew_seconds=future_skew_seconds,
        domain=SIGNING_DOMAIN,
    )
    if envelope_check.get("valid") is not True:
        return {
            "valid": False,
            "blockers": sorted({"GOVERNANCE_" + code for code in envelope_check.get("blockers", [])}),
        }
    payload_check = validate_governance_payload(
        wrapper["payload"], now=now, max_age_seconds=max_age_seconds,
        future_skew_seconds=future_skew_seconds,
    )
    payload_check["issuer"] = envelope_check.get("issuer")
    payload_check["nonce"] = envelope_check.get("nonce")
    payload_check["envelope_issued_at"] = envelope_check.get("issued_at")
    return payload_check


def sign_attestation(
    payload: Mapping[str, Any],
    *,
    key: str | bytes | bytearray | None,
    issuer: str,
    issued_at: datetime | None = None,
    nonce: str | None = None,
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Producer helper: wrap an attestation in the governance signing domain."""
    return {
        "payload": dict(payload),
        "envelope": make_envelope(
            dict(payload), key=key, issuer=issuer, issued_at=issued_at,
            nonce=nonce, ttl_seconds=ttl_seconds, domain=SIGNING_DOMAIN,
        ),
    }


def apply_attested_calibration(
    verdicts: Sequence[Mapping[str, Any]],
    attestation: Mapping[str, Any] | None,
    *,
    issuer: str,
) -> list[Any]:
    """Replace caller-supplied calibration metadata with the attested per-agent report.

    Agent output is untrusted for calibration: an agent cannot assert its own Brier
    score. Both the pipeline and the HTTP policy API call this after verifying the
    governance attestation, so ``calibration_meta`` in the bundle never reaches a gate.
    """
    attested: Mapping[str, Any] = {}
    method_version = None
    if isinstance(attestation, Mapping) and attestation.get("valid") is True:
        raw = attestation.get("agent_calibration")
        attested = raw if isinstance(raw, Mapping) else {}
        method_version = attestation.get("method_version")
    output: list[Any] = []
    for verdict in verdicts:
        if not isinstance(verdict, Mapping):
            output.append(verdict)
            continue
        agent_id = str(verdict.get("agent_id", "")).strip()
        row = attested.get(agent_id)
        copy = dict(verdict)
        if isinstance(row, Mapping):
            copy["calibration_meta"] = {
                "brier_score": row.get("brier_score"),
                "sample_size": row.get("sample_size"),
                "calibration_status": row.get("calibration_status"),
                "method_version": method_version,
                "source": str(issuer),
            }
        else:
            copy["calibration_meta"] = None
        output.append(copy)
    return output
