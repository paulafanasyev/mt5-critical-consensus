"""Deterministic orchestration of already-produced agent outputs.

No LLM, market-data ingestion, broker API, or order dispatch occurs here. This module
validates the hand-offs between gates and creates an in-memory hash-linked decision trace.
Durable append-only persistence must be supplied by the deployment adapter.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from typing import Any, Mapping, Sequence

from .audit import canonical_hash, make_audit_event, verify_audit_chain
from .calibration import weighted_consensus
from .evidence import EvidenceError, key_bytes, verify_bundle, verify_decision, verify_review
from .gates import certainty_gate, chief_skeptic_pass, permission_gate, user_authorization_gate
from .governance import apply_attested_calibration, verify_governance_attestation
from .risk_controls import risk_controlled_permission

_VERDICT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERDICT_FUTURE_SKEW_SECONDS = 30
_DEFAULT_MAX_EVIDENCE_AGE_SECONDS = 3600


def _is_timestamp(value: Any, current: datetime, *, future_skew_seconds: int = 30,
                  max_age_seconds: float | None = None) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return False
    age = (current - parsed.astimezone(timezone.utc)).total_seconds()
    if age < -future_skew_seconds:
        return False
    return max_age_seconds is None or age <= max_age_seconds


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _as_records(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _policy_seconds(policy: Mapping[str, Any], key: str,
                    default: float = _DEFAULT_MAX_EVIDENCE_AGE_SECONDS) -> float:
    thresholds = policy.get("certainty_thresholds", policy) if isinstance(policy, Mapping) else {}
    raw = thresholds.get(key, default) if isinstance(thresholds, Mapping) else default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default
    value = float(raw)
    return value if math.isfinite(value) and value > 0 else default


def _validate_verdict_metadata(
    verdict: Mapping[str, Any], signal_id: str, current: datetime
) -> list[str]:
    """Bind a verdict to this signal and reject malformed or stale metadata."""
    blockers: list[str] = []
    if not str(verdict.get("verdict_id", "")).strip():
        blockers.append("VERDICT_ID_MISSING")
    if not str(verdict.get("agent_id", "")).strip():
        blockers.append("VERDICT_AGENT_ID_MISSING")
    if not signal_id or str(verdict.get("signal_id", "")).strip() != signal_id:
        blockers.append("VERDICT_SIGNAL_ID_MISMATCH")

    input_hash = verdict.get("input_hash")
    if not isinstance(input_hash, str) or _VERDICT_HASH_RE.fullmatch(input_hash) is None:
        blockers.append("VERDICT_INPUT_HASH_INVALID")

    raw_ttl = verdict.get("ttl_seconds")
    if isinstance(raw_ttl, bool) or not isinstance(raw_ttl, int) or raw_ttl <= 0:
        blockers.append("VERDICT_TTL_INVALID")
        ttl_seconds = None
    else:
        ttl_seconds = raw_ttl

    raw_created_at = verdict.get("created_at")
    created_at: datetime | None = None
    if not isinstance(raw_created_at, str) or not raw_created_at.strip():
        blockers.append("VERDICT_CREATED_AT_MISSING_OR_INVALID")
    else:
        try:
            parsed = datetime.fromisoformat(raw_created_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("timezone required")
            created_at = parsed.astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            blockers.append("VERDICT_CREATED_AT_MISSING_OR_INVALID")

    if created_at is not None:
        age_seconds = (current - created_at).total_seconds()
        if age_seconds < -_VERDICT_FUTURE_SKEW_SECONDS:
            blockers.append("VERDICT_CREATED_AT_IN_FUTURE")
        elif ttl_seconds is not None and age_seconds > ttl_seconds:
            blockers.append("VERDICT_EXPIRED")
    return blockers


def review_attestation_payload(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """The exact review-service view a review attestation must cover."""
    return {"signal_id": bundle.get("signal_id"),
            "verdicts": bundle.get("verdicts"),
            "peer_reviews": bundle.get("peer_reviews"),
            "challenge_records": bundle.get("challenge_records"),
            "chief_reasoning": bundle.get("chief_reasoning")}


def permission_tombstone_scope(request: Mapping[str, Any]) -> str:
    """Server-derived identity of the approved order.

    Caller-chosen fields (permission_id, expiry, timestamps) are excluded so that renaming or
    re-issuing a permission cannot reset the once-per-order authorization tombstone.
    """
    sl_tp = request.get("sl_tp")
    if not isinstance(sl_tp, Mapping):
        raise EvidenceError("permission request has no SL/TP geometry")
    return canonical_hash({
        "signal_id": str(request.get("signal_id", "")).strip(),
        "instrument": str(request.get("instrument", "")).strip().upper(),
        "direction": str(request.get("direction", "")).strip().upper(),
        "size": request.get("size"),
        "expected_price": request.get("expected_price"),
        "sl": sl_tp.get("sl"),
        "tp": sl_tp.get("tp"),
        "mode": str(request.get("mode", "")).strip().lower(),
    })


def evaluate_signal_bundle(
    bundle: Mapping[str, Any],
    certainty_policy: Mapping[str, Any],
    permission_matrix: Mapping[str, Any],
    *,
    now: datetime | None = None,
    require_persisted_risk: bool = False,
    kill_switch_state: Mapping[str, Any] | None = None,
    terminal_risk_snapshot: Mapping[str, Any] | None = None,
    evidence_key: str | bytes | bytearray | None = None,
    governance_key: str | bytes | bytearray | None = None,
    expected_evidence_issuer: str | None = None,
    expected_governance_issuer: str = "calibration-service",
    authorization_store: Any | None = None,
    operator_key: str | bytes | bytearray | None = None,
    expected_operator_issuer: str | None = None,
    operator_decision_envelope: Mapping[str, Any] | None = None,
    review_key: str | bytes | bytearray | None = None,
    expected_review_issuer: str | None = None,
) -> dict[str, Any]:
    """Evaluate certainty → ChiefAgent → Permission → user authorization, fail-closed.

    The function returns no OrderCommand and always sets ``trade_allowed`` to false.
    The bundle must carry an HMAC evidence envelope produced by the trusted ingress;
    historical governance metrics must carry a separately signed calibration-service
    attestation. Peer-review inputs are expected to come from the trusted internal
    review service; the bridge API must not accept user-supplied review records as
    authoritative evidence.
    """
    if not isinstance(bundle, Mapping):
        return {"status": "GATHER_MORE", "trade_allowed": False, "blockers": ["INVALID_SIGNAL_BUNDLE"], "audit_events": []}

    try:
        current = datetime.now(timezone.utc) if now is None else now
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("timezone required")
        current = current.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return {"status": "GATHER_MORE", "trade_allowed": False, "blockers": ["INVALID_CURRENT_TIME"], "audit_events": []}

    signal_id = str(bundle.get("signal_id", "")).strip()
    try:
        bundle_hash = canonical_hash(bundle)
    except (TypeError, ValueError, OverflowError):
        return {"signal_id": signal_id or "unknown", "status": "GATHER_MORE",
                "trade_allowed": False, "blockers": ["SIGNAL_BUNDLE_NOT_CANONICALLY_HASHABLE"],
                "audit_events": [], "audit_chain_valid": False}

    trace: list[dict[str, Any]] = []
    previous_hash: str | None = None

    def emit(event_type: str, payload: Mapping[str, Any]) -> None:
        nonlocal previous_hash
        event = make_audit_event(event_type, payload, previous_hash)
        trace.append(event)
        previous_hash = event["event_hash"]

    def finish(status: str, blockers: list[str], **details: Any) -> dict[str, Any]:
        emit("PIPELINE_RESULT", {"signal_id": signal_id or "unknown", "status": status,
                                 "blockers": sorted(set(blockers)),
                                 "detail_hash": canonical_hash(details)})
        chain_check = verify_audit_chain(trace)
        return {"signal_id": signal_id or "unknown", "status": status, "trade_allowed": False,
                "blockers": sorted(set(blockers)), "details": details,
                "audit_events": trace, "audit_chain_valid": chain_check.get("valid") is True,
                "audit_chain_check": chain_check}

    emit("SIGNAL_RECEIVED", {"signal_id": signal_id or "unknown", "bundle_hash": bundle_hash})

    config_blockers: list[str] = []
    if not isinstance(expected_evidence_issuer, str) or not expected_evidence_issuer.strip():
        config_blockers.append("EVIDENCE_EXPECTED_ISSUER_NOT_CONFIGURED")
    if not isinstance(expected_governance_issuer, str) or not expected_governance_issuer.strip():
        config_blockers.append("GOVERNANCE_EXPECTED_ISSUER_NOT_CONFIGURED")
    elif (isinstance(expected_evidence_issuer, str) and expected_evidence_issuer.strip()
          and expected_evidence_issuer.strip() == expected_governance_issuer.strip()):
        config_blockers.append("GOVERNANCE_ISSUER_NOT_DISTINCT_FROM_EVIDENCE_ISSUER")
    if not isinstance(expected_review_issuer, str) or not expected_review_issuer.strip():
        config_blockers.append("REVIEW_EXPECTED_ISSUER_NOT_CONFIGURED")
    else:
        review_issuer_value = expected_review_issuer.strip()
        reserved_issuers = {str(expected_evidence_issuer or "").strip(),
                            str(expected_governance_issuer or "").strip(),
                            str(expected_operator_issuer or "").strip()} - {""}
        if review_issuer_value in reserved_issuers:
            config_blockers.append("REVIEW_ISSUER_NOT_DISTINCT")
    try:
        evidence_key_bytes = key_bytes(evidence_key)
        governance_key_bytes = key_bytes(governance_key)
        if evidence_key_bytes == governance_key_bytes:
            config_blockers.append("GOVERNANCE_KEY_NOT_DISTINCT_FROM_EVIDENCE_KEY")
        review_key_bytes = key_bytes(review_key)
        if review_key_bytes in (evidence_key_bytes, governance_key_bytes):
            config_blockers.append("REVIEW_KEY_NOT_DISTINCT")
        if operator_key is not None and review_key_bytes == key_bytes(operator_key):
            config_blockers.append("REVIEW_KEY_NOT_DISTINCT")
    except EvidenceError:
        pass  # unusable keys are reported by envelope verification below
    if config_blockers:
        return finish("GATHER_MORE", config_blockers)

    envelope_check = verify_bundle(
        bundle, key=evidence_key, expected_issuer=expected_evidence_issuer, now=current,
    )
    emit("EVIDENCE_ENVELOPE", {
        "signal_id": signal_id or "unknown",
        "valid": envelope_check.get("valid") is True,
        "issuer": envelope_check.get("issuer"),
        "payload_sha256": envelope_check.get("payload_sha256"),
        "blockers": list(envelope_check.get("blockers", [])),
    })
    if envelope_check.get("valid") is not True:
        return finish("GATHER_MORE", list(envelope_check.get("blockers", [])),
                      evidence_envelope=envelope_check)

    signal_input = bundle.get("signal_input")
    supplied_signal_hash = bundle.get("signal_input_hash")
    if not isinstance(signal_input, Mapping) or not signal_id:
        emit("SIGNAL_INPUT_BINDING", {"signal_id": signal_id or "unknown", "status": "FAILED",
                                      "reason": "SIGNAL_INPUT_OR_HASH_MISSING"})
        return finish("GATHER_MORE", ["SIGNAL_INPUT_OR_HASH_MISSING"])
    binding_blockers: list[str] = []
    if str(signal_input.get("signal_id", "")).strip() != signal_id:
        binding_blockers.append("SIGNAL_INPUT_SIGNAL_ID_MISMATCH")
    signal_instrument = str(signal_input.get("instrument", "")).strip().upper()
    signal_direction = str(signal_input.get("direction", "")).strip().upper()
    if not signal_instrument:
        binding_blockers.append("SIGNAL_INPUT_INSTRUMENT_MISSING")
    if signal_direction not in {"BUY", "SELL"}:
        binding_blockers.append("SIGNAL_INPUT_DIRECTION_INVALID")
    if binding_blockers:
        emit("SIGNAL_INPUT_BINDING", {"signal_id": signal_id, "status": "FAILED",
                                      "blockers": sorted(set(binding_blockers))})
        return finish("GATHER_MORE", binding_blockers)
    try:
        expected_signal_hash = canonical_hash(signal_input)
    except (TypeError, ValueError, OverflowError):
        emit("SIGNAL_INPUT_BINDING", {"signal_id": signal_id, "status": "FAILED",
                                      "reason": "SIGNAL_INPUT_NOT_CANONICALLY_HASHABLE"})
        return finish("GATHER_MORE", ["SIGNAL_INPUT_NOT_CANONICALLY_HASHABLE"])
    if supplied_signal_hash != expected_signal_hash:
        emit("SIGNAL_INPUT_BINDING", {"signal_id": signal_id, "status": "FAILED",
                                      "reason": "SIGNAL_INPUT_HASH_MISMATCH"})
        return finish("GATHER_MORE", ["SIGNAL_INPUT_HASH_MISMATCH"])
    emit("SIGNAL_INPUT_BINDING", {"signal_id": signal_id, "status": "PASSED",
                                  "signal_input_hash": expected_signal_hash})
    raw_evidence_refs = signal_input.get("available_evidence_refs")
    if (not isinstance(raw_evidence_refs, Sequence) or isinstance(raw_evidence_refs, (str, bytes))
            or not raw_evidence_refs or any(not isinstance(item, str) or not item.strip() for item in raw_evidence_refs)
            or len(set(raw_evidence_refs)) != len(raw_evidence_refs)):
        emit("SIGNAL_EVIDENCE_REGISTRY", {"signal_id": signal_id, "status": "FAILED",
                                         "reason": "SIGNAL_EVIDENCE_REGISTRY_MISSING_INVALID_OR_DUPLICATE"})
        return finish("GATHER_MORE", ["SIGNAL_EVIDENCE_REGISTRY_MISSING_INVALID_OR_DUPLICATE"])
    allowed_evidence_refs = {item.strip() for item in raw_evidence_refs}
    emit("SIGNAL_EVIDENCE_REGISTRY", {"signal_id": signal_id, "status": "PASSED",
                                     "evidence_ref_count": len(allowed_evidence_refs)})

    # Peer reviews, challenge records and Chief reasoning are review-service output, not
    # ingress evidence. A holder of the evidence key must not be able to mint them, and
    # binding signal_id prevents replaying an attested review snapshot under another signal.
    review_check = verify_review(
        review_attestation_payload(bundle), bundle.get("review_attestation"), key=review_key,
        expected_issuer=expected_review_issuer, now=current,
    )
    emit("REVIEW_ATTESTATION", {
        "signal_id": signal_id, "valid": review_check.get("valid") is True,
        "issuer": review_check.get("issuer"),
        "payload_sha256": review_check.get("payload_sha256"),
        "blockers": list(review_check.get("blockers", [])),
    })
    if review_check.get("valid") is not True:
        return finish("GATHER_MORE", list(review_check.get("blockers", [])),
                      review_attestation=review_check)

    raw_verdicts = bundle.get("verdicts")
    verdicts = [dict(v) for v in _as_records(raw_verdicts)]
    if not verdicts:
        emit("VERDICT_VALIDATION", {"signal_id": signal_id or "unknown", "status": "FAILED"})
        return finish("GATHER_MORE", ["VERDICTS_MISSING_OR_INVALID"])

    metadata_blockers: list[str] = []
    if not signal_id:
        metadata_blockers.append("SIGNAL_ID_MISSING")
    if not isinstance(raw_verdicts, Sequence) or isinstance(raw_verdicts, (str, bytes)) or len(verdicts) != len(raw_verdicts):
        metadata_blockers.append("VERDICT_RECORD_INVALID")
    for index, verdict in enumerate(verdicts):
        metadata_blockers.extend(
            f"VERDICT_{index}:{blocker}"
            for blocker in _validate_verdict_metadata(verdict, signal_id, current)
        )
        if verdict.get("input_hash") != expected_signal_hash:
            metadata_blockers.append(f"VERDICT_{index}:VERDICT_INPUT_HASH_DOES_NOT_MATCH_SIGNAL_INPUT")
        verdict_refs = verdict.get("evidence_refs")
        if (not isinstance(verdict_refs, Sequence) or isinstance(verdict_refs, (str, bytes))
                or not verdict_refs or any(str(ref).strip() not in allowed_evidence_refs for ref in verdict_refs)):
            metadata_blockers.append(f"VERDICT_{index}:VERDICT_EVIDENCE_NOT_IN_SIGNAL_REGISTRY")
    if metadata_blockers:
        emit("VERDICT_METADATA_VALIDATION", {"signal_id": signal_id or "unknown", "status": "FAILED",
                                               "blockers": sorted(set(metadata_blockers))})
        return finish("GATHER_MORE", metadata_blockers,
                      verdict_metadata_validation={"status": "FAILED", "blockers": sorted(set(metadata_blockers))})
    emit("VERDICT_METADATA_VALIDATION", {"signal_id": signal_id, "status": "PASSED",
                                           "verdict_count": len(verdicts)})

    max_review_age = _policy_seconds(certainty_policy, "max_review_age_seconds")
    max_challenge_age = _policy_seconds(certainty_policy, "max_challenge_age_seconds")
    max_chief_age = _policy_seconds(certainty_policy, "max_chief_reasoning_age_seconds",
                                    default=900.0)
    reviews = _as_records(bundle.get("peer_reviews"))
    review_index: dict[str, set[str]] = {}
    review_blockers: list[str] = []
    challenge_blockers: list[str] = []
    challenge_reviews: list[Mapping[str, Any]] = []
    seen_review_ids: set[str] = set()
    verdict_authors = {str(verdict.get("verdict_id", "")).strip(): str(verdict.get("agent_id", "")).strip()
                       for verdict in verdicts}
    for review in reviews:
        review_id = str(review.get("review_id", "")).strip()
        target_id = str(review.get("target_verdict_id", "")).strip()
        reviewer_alias = str(review.get("reviewer_alias", "")).strip()
        stance = str(review.get("stance", "")).upper()
        evidence_refs = review.get("evidence_refs")
        evidence_ok = (isinstance(evidence_refs, Sequence) and not isinstance(evidence_refs, (str, bytes))
                       and bool(evidence_refs)
                       and all(str(x).strip() and str(x).strip() in allowed_evidence_refs for x in evidence_refs))
        argument = str(review.get("argument", "")).strip()
        challenge_type = str(review.get("challenge_type", "")).upper()
        valid_challenge_types = {"DATA_CONTRADICTION", "SOURCE_WEAK", "STALE_DATA", "LOGIC_GAP", "REGIME_MISMATCH", "CORRELATION_RISK"}
        challenge_type_ok = stance != "CHALLENGE" or challenge_type in valid_challenge_types
        created_ok = _is_timestamp(review.get("created_at"), current, max_age_seconds=max_review_age)
        if not review_id or review_id in seen_review_ids:
            review_blockers.append("MISSING_OR_DUPLICATE_REVIEW_ID")
            continue
        seen_review_ids.add(review_id)
        if (review.get("status") != "VALIDATED" or not target_id or not reviewer_alias
                or stance not in {"CONFIRM", "CHALLENGE"} or not evidence_ok or not argument
                or not challenge_type_ok or not created_ok):
            review_blockers.append("INVALID_PEER_REVIEW_RECORD")
            continue
        target_set = review_index.setdefault(target_id, set())
        if reviewer_alias in target_set:
            review_blockers.append("DUPLICATE_PEER_REVIEWER_FOR_VERDICT")
            continue
        if verdict_authors.get(target_id) == reviewer_alias:
            review_blockers.append(f"PEER_REVIEWER_IS_VERDICT_AUTHOR:{target_id}")
            continue
        target_set.add(reviewer_alias)
        if stance == "CHALLENGE":
            challenge_reviews.append(review)

    verdict_ids: set[str] = set()
    for verdict in verdicts:
        verdict_id = str(verdict.get("verdict_id", "")).strip()
        if not verdict_id or verdict_id in verdict_ids:
            review_blockers.append("MISSING_OR_DUPLICATE_VERDICT_ID")
        if verdict_id:
            verdict_ids.add(verdict_id)
        verdict["reviewer_count"] = len(review_index.get(verdict_id, set()))
    if any(target_id not in verdict_ids for target_id in review_index):
        review_blockers.append("PEER_REVIEW_TARGET_NOT_FOUND")

    raw_challenge_records = bundle.get("challenge_records")
    if "challenge_records" not in bundle:
        challenge_blockers.append("CHALLENGE_LEDGER_MISSING")
        challenge_records = []
    elif not isinstance(raw_challenge_records, Sequence) or isinstance(raw_challenge_records, (str, bytes)):
        challenge_blockers.append("CHALLENGE_LEDGER_INVALID")
        challenge_records = []
    else:
        challenge_records = _as_records(raw_challenge_records)
        if len(challenge_records) != len(raw_challenge_records):
            challenge_blockers.append("CHALLENGE_LEDGER_HAS_INVALID_RECORDS")
    seen_challenge_ids: set[str] = set()
    for challenge_index, challenge in enumerate(challenge_records):
        challenge_id = str(challenge.get("challenge_id", "")).strip()
        if not challenge_id or challenge_id in seen_challenge_ids:
            challenge_blockers.append(f"CHALLENGE_{challenge_index}_ID_MISSING_OR_DUPLICATE")
        else:
            seen_challenge_ids.add(challenge_id)
        if not str(challenge.get("target_verdict_id", "")).strip():
            challenge_blockers.append(f"CHALLENGE_{challenge_index}_TARGET_MISSING")
        if not str(challenge.get("argument", "")).strip():
            challenge_blockers.append(f"CHALLENGE_{challenge_index}_ARGUMENT_MISSING")
        rounds = challenge.get("rounds")
        if (not isinstance(rounds, Sequence) or isinstance(rounds, (str, bytes)) or len(rounds) > 3):
            challenge_blockers.append(f"CHALLENGE_{challenge_index}_ROUNDS_INVALID")
        if not _is_timestamp(challenge.get("created_at"), current, max_age_seconds=max_challenge_age):
            challenge_blockers.append(f"CHALLENGE_{challenge_index}_CREATED_AT_INVALID")
        if str(challenge.get("status", "OPEN")).upper() not in {"OPEN", "RESOLVED", "ESCALATED"}:
            challenge_blockers.append(f"CHALLENGE_{challenge_index}_STATUS_INVALID")
        refs = challenge.get("evidence_refs", [])
        resolution_refs = challenge.get("resolution_evidence_refs", [])
        if (not isinstance(refs, Sequence) or isinstance(refs, (str, bytes))
                or any(str(ref).strip() not in allowed_evidence_refs for ref in refs)):
            challenge_blockers.append(f"CHALLENGE_{challenge_index}_EVIDENCE_NOT_IN_SIGNAL_REGISTRY")
        if (not isinstance(resolution_refs, Sequence) or isinstance(resolution_refs, (str, bytes))
                or any(str(ref).strip() not in allowed_evidence_refs for ref in resolution_refs)):
            challenge_blockers.append(f"CHALLENGE_{challenge_index}_RESOLUTION_EVIDENCE_NOT_IN_SIGNAL_REGISTRY")

    registered_challenges = {(str(ch.get("target_verdict_id", "")), str(ch.get("challenge_type", "")).upper()) for ch in challenge_records}
    for review in challenge_reviews:
        key = (str(review.get("target_verdict_id", "")), str(review.get("challenge_type", "")).upper())
        if key not in registered_challenges:
            review_blockers.append("PEER_CHALLENGE_NOT_REGISTERED_IN_CHALLENGE_LEDGER")

    open_challenges: list[Mapping[str, Any]] = []
    for challenge in challenge_records:
        status_value = str(challenge.get("status", "OPEN")).upper()
        if status_value == "RESOLVED":
            resolution_refs = challenge.get("resolution_evidence_refs")
            initial_refs = challenge.get("evidence_refs")
            resolution_ok = (
                isinstance(resolution_refs, Sequence) and not isinstance(resolution_refs, (str, bytes))
                and bool(resolution_refs)
                and all(str(ref).strip() in allowed_evidence_refs for ref in resolution_refs)
            )
            initial_set = ({str(ref).strip() for ref in initial_refs}
                           if isinstance(initial_refs, Sequence) and not isinstance(initial_refs, (str, bytes))
                           else set())
            new_evidence = resolution_ok and bool({str(ref).strip() for ref in resolution_refs} - initial_set)
            if not resolution_ok or not new_evidence:
                challenge_blockers.append("CHALLENGE_RESOLUTION_MISSING_NEW_EVIDENCE")
                open_challenges.append({**dict(challenge), "status": "OPEN"})
        else:
            open_challenges.append(dict(challenge))
    supplied_open = bundle.get("open_challenges", [])
    if isinstance(supplied_open, Sequence) and not isinstance(supplied_open, (str, bytes)):
        known = {str(c.get("challenge_id", "")) for c in open_challenges if isinstance(c, Mapping)}
        for challenge in supplied_open:
            if isinstance(challenge, Mapping) and str(challenge.get("challenge_id", "")) not in known:
                open_challenges.append(challenge)
    elif "open_challenges" in bundle:
        challenge_blockers.append("INVALID_OPEN_CHALLENGE_STATE")

    resolution_status = str(bundle.get("challenge_resolution_status", "")).upper()
    if resolution_status == "DEADLOCK":
        pass
    elif open_challenges:
        resolution_status = "OPEN"
    elif challenge_records:
        resolution_status = "RESOLVED"
    elif resolution_status not in {"RESOLVED", "NONE"}:
        resolution_status = "MISSING"

    try:
        consensus_score = weighted_consensus(verdicts)
    except (TypeError, ValueError, KeyError, OverflowError):
        consensus_score = 0.0
        review_blockers.append("CALIBRATED_CONSENSUS_COULD_NOT_BE_COMPUTED")

    risk_flags = bundle.get("risk_flags", [])
    if not isinstance(risk_flags, Sequence) or isinstance(risk_flags, (str, bytes)):
        risk_flags = ["INVALID_RISK_FLAGS"]
    all_risk_flags = [str(x) for x in risk_flags if str(x).strip()]
    all_risk_flags.extend(review_blockers)
    all_risk_flags.extend(challenge_blockers)

    raw_key_sources = bundle.get("key_sources", [])
    if isinstance(raw_key_sources, Sequence) and not isinstance(raw_key_sources, (str, bytes)):
        for source in raw_key_sources:
            if isinstance(source, Mapping):
                source_id = str(source.get("source_id", "")).strip()
                if not source_id or source_id not in allowed_evidence_refs:
                    all_risk_flags.append("KEY_SOURCE_NOT_IN_SIGNAL_EVIDENCE_REGISTRY:" + (source_id or "missing"))

    devil_report = bundle.get("devil_advocate_report")
    if isinstance(devil_report, Mapping):
        hypotheses = devil_report.get("counter_hypotheses", [])
        if isinstance(hypotheses, Sequence) and not isinstance(hypotheses, (str, bytes)):
            for hypothesis_index, hypothesis in enumerate(hypotheses):
                if not isinstance(hypothesis, Mapping):
                    all_risk_flags.append(f"DEVIL_ADVOCATE_HYPOTHESIS_{hypothesis_index}_INVALID")
                    continue
                refs = hypothesis.get("evidence_refs")
                if (not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)) or not refs
                        or any(str(ref).strip() not in allowed_evidence_refs for ref in refs)):
                    all_risk_flags.append(f"DEVIL_ADVOCATE_HYPOTHESIS_{hypothesis_index}_EVIDENCE_NOT_IN_SIGNAL_REGISTRY")

    governance_check = verify_governance_attestation(
        bundle.get("governance_attestation"),
        key=governance_key,
        expected_issuer=expected_governance_issuer,
        now=current,
    )
    emit("GOVERNANCE_ATTESTATION", {
        "signal_id": signal_id,
        "valid": governance_check.get("valid") is True,
        "issuer": governance_check.get("issuer"),
        "observed_at": governance_check.get("observed_at"),
        "blockers": list(governance_check.get("blockers", [])),
    })
    if governance_check.get("valid") is not True:
        return finish("GATHER_MORE", list(governance_check.get("blockers", [])),
                      governance_attestation=governance_check)

    attested_verdicts = apply_attested_calibration(
        verdicts, governance_check, issuer=str(expected_governance_issuer).strip(),
    )
    verdicts = [dict(row) if isinstance(row, Mapping) else row for row in attested_verdicts]
    emit("GOVERNANCE_CALIBRATION", {
        "signal_id": signal_id,
        "attested_agents": len(governance_check.get("agent_calibration") or {}),
        "method_version": governance_check.get("method_version"),
    })
    try:
        consensus_score = weighted_consensus(verdicts)
    except (TypeError, ValueError, KeyError, OverflowError):
        consensus_score = 0.0
        all_risk_flags.append("CALIBRATED_CONSENSUS_COULD_NOT_BE_COMPUTED")

    certainty_input = {
        "verdicts": verdicts,
        "consensus_score": consensus_score,
        "open_challenges": open_challenges,
        "challenge_resolution_status": resolution_status,
        "contradictions_last_n_hours": bundle.get("contradictions_last_n_hours"),
        "contradiction_lookback_hours": bundle.get("contradiction_lookback_hours"),
        "key_sources": bundle.get("key_sources", []),
        "news_or_rumor": bundle.get("news_or_rumor"),
        "devil_advocate_report": bundle.get("devil_advocate_report"),
        "risk_flags": all_risk_flags,
    }
    certainty = certainty_gate(certainty_input, certainty_policy, governance=governance_check,
                               now=current)
    emit("CERTAINTY_GATE", {"signal_id": signal_id, "status": certainty.get("status"),
                             "blockers": certainty.get("blockers", []), "consensus_score": consensus_score})
    if certainty.get("status") == "ESCALATE":
        return finish("ESCALATE", list(certainty.get("blockers", [])), certainty=certainty)
    if certainty.get("status") != "PASS":
        return finish("GATHER_MORE", list(certainty.get("blockers", [])), certainty=certainty)

    chief_reasoning = bundle.get("chief_reasoning")
    if not isinstance(chief_reasoning, Mapping):
        emit("CHIEF_REVIEW", {"signal_id": signal_id, "status": "HOLD", "reason": "CHIEF_REASONING_MISSING"})
        return finish("HOLD", ["CHIEF_REASONING_MISSING"], certainty=certainty)
    chief_input = dict(chief_reasoning)
    if str(chief_input.get("signal_id", "")) != signal_id:
        emit("CHIEF_REVIEW", {"signal_id": signal_id, "status": "HOLD", "reason": "CHIEF_SIGNAL_ID_MISMATCH"})
        return finish("HOLD", ["CHIEF_SIGNAL_ID_MISMATCH"], certainty=certainty)
    actual_challenge_snapshot = sorted((dict(ch) for ch in challenge_records), key=lambda row: str(row.get("challenge_id", "")))
    try:
        snapshot_hash = canonical_hash(chief_input.get("challenge_ledger_snapshot", []))
        actual_snapshot_hash = canonical_hash(actual_challenge_snapshot)
    except (TypeError, ValueError, OverflowError):
        emit("CHIEF_REVIEW", {"signal_id": signal_id, "status": "HOLD",
                              "reason": "CHIEF_CHALLENGE_LEDGER_SNAPSHOT_INVALID"})
        return finish("HOLD", ["CHIEF_CHALLENGE_LEDGER_SNAPSHOT_INVALID"], certainty=certainty)
    if snapshot_hash != actual_snapshot_hash:
        emit("CHIEF_REVIEW", {"signal_id": signal_id, "status": "HOLD", "reason": "CHIEF_CHALLENGE_LEDGER_SNAPSHOT_MISMATCH"})
        return finish("HOLD", ["CHIEF_CHALLENGE_LEDGER_SNAPSHOT_MISMATCH"], certainty=certainty)
    supplied_reasoning_hash = chief_input.get("reasoning_hash")
    unsigned_chief = dict(chief_input)
    unsigned_chief.pop("reasoning_hash", None)
    try:
        expected_hash = canonical_hash(unsigned_chief)
    except (TypeError, ValueError, OverflowError):
        return finish("HOLD", ["CHIEF_REASONING_NOT_CANONICALLY_HASHABLE"], certainty=certainty)
    if not isinstance(supplied_reasoning_hash, str) or not supplied_reasoning_hash.strip():
        emit("CHIEF_REVIEW", {"signal_id": signal_id, "status": "HOLD", "reason": "CHIEF_REASONING_HASH_MISSING"})
        return finish("HOLD", ["CHIEF_REASONING_HASH_MISSING"], certainty=certainty)
    if supplied_reasoning_hash != expected_hash:
        emit("CHIEF_REVIEW", {"signal_id": signal_id, "status": "HOLD", "reason": "CHIEF_REASONING_HASH_MISMATCH"})
        return finish("HOLD", ["CHIEF_REASONING_HASH_MISMATCH"], certainty=certainty)
    chief_evidence_blockers: list[str] = []
    chief_steps = chief_input.get("steps", {})
    if isinstance(chief_steps, Mapping):
        for step_name, step in chief_steps.items():
            if not isinstance(step, Mapping):
                chief_evidence_blockers.append(f"CHIEF_STEP_{step_name}_INVALID")
                continue
            refs = step.get("evidence_refs")
            if (not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)) or not refs
                    or any(str(ref).strip() not in allowed_evidence_refs for ref in refs)):
                chief_evidence_blockers.append(f"CHIEF_STEP_{step_name}_EVIDENCE_NOT_IN_SIGNAL_REGISTRY")
    chief_scenarios = chief_input.get("tail_risk_scenarios", [])
    if isinstance(chief_scenarios, Sequence) and not isinstance(chief_scenarios, (str, bytes)):
        for scenario_index, scenario in enumerate(chief_scenarios):
            if not isinstance(scenario, Mapping):
                chief_evidence_blockers.append(f"CHIEF_TAIL_SCENARIO_{scenario_index}_INVALID")
                continue
            refs = scenario.get("evidence_refs")
            if (not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)) or not refs
                    or any(str(ref).strip() not in allowed_evidence_refs for ref in refs)):
                chief_evidence_blockers.append(f"CHIEF_TAIL_SCENARIO_{scenario_index}_EVIDENCE_NOT_IN_SIGNAL_REGISTRY")
    if chief_evidence_blockers:
        emit("CHIEF_EVIDENCE_VALIDATION", {"signal_id": signal_id, "status": "HOLD",
                                           "blockers": sorted(set(chief_evidence_blockers))})
        return finish("HOLD", chief_evidence_blockers, certainty=certainty)
    chief = chief_skeptic_pass(chief_input, now=current, max_age_seconds=max_chief_age)
    emit("CHIEF_REVIEW", {"signal_id": signal_id, "status": chief.get("verdict"),
                          "reasoning_hash": chief.get("reasoning_hash"), "blockers": chief.get("blockers", [])})
    if chief.get("verdict") == "REJECT":
        return finish("REJECT", list(chief.get("blockers", [])), certainty=certainty, chief=chief)
    if chief.get("verdict") != "CONFIRM":
        return finish("HOLD", list(chief.get("blockers", [])), certainty=certainty, chief=chief)

    permission_request = bundle.get("permission_request")
    if not isinstance(permission_request, Mapping):
        emit("PERMISSION_GATE", {"signal_id": signal_id, "status": "DENY", "reason": "PERMISSION_REQUEST_MISSING"})
        return finish("DENY", ["PERMISSION_REQUEST_MISSING"], certainty=certainty, chief=chief)
    req = dict(permission_request)
    metadata_blockers: list[str] = []
    if str(req.get("signal_id", "")) != signal_id:
        metadata_blockers.append("PERMISSION_SIGNAL_ID_MISMATCH")
    if str(req.get("instrument", "")).strip().upper() != signal_instrument:
        metadata_blockers.append("PERMISSION_INSTRUMENT_SIGNAL_MISMATCH")
    if str(req.get("direction", "")).strip().upper() != signal_direction:
        metadata_blockers.append("PERMISSION_DIRECTION_SIGNAL_MISMATCH")
    if req.get("chief_reasoning_hash") != chief.get("reasoning_hash"):
        metadata_blockers.append("PERMISSION_CHIEF_REASONING_HASH_MISMATCH")
    try:
        raw_permission_score = req.get("consensus_score")
        permission_score = float(raw_permission_score) if not isinstance(raw_permission_score, bool) else float("nan")
    except (TypeError, ValueError, OverflowError):
        permission_score = float("nan")
    if not math.isfinite(permission_score) or abs(permission_score - consensus_score) > 1e-9:
        metadata_blockers.append("PERMISSION_CONSENSUS_SCORE_MISMATCH")
    source_rows = req.get("sources")
    key_sources = bundle.get("key_sources", [])
    if not isinstance(source_rows, Sequence) or isinstance(source_rows, (str, bytes)) or not isinstance(key_sources, Sequence) or isinstance(key_sources, (str, bytes)):
        metadata_blockers.append("PERMISSION_SOURCE_SUMMARY_MISSING_OR_INVALID")
    else:
        expected_sources = {
            str(source.get("source_id", "")): source
            for source in key_sources if isinstance(source, Mapping) and str(source.get("source_id", "").strip())
        }
        supplied_sources: dict[str, Mapping[str, Any]] = {}
        for source in source_rows:
            if not isinstance(source, Mapping):
                metadata_blockers.append("PERMISSION_SOURCE_SUMMARY_INVALID")
                continue
            source_id = str(source.get("id", "")).strip()
            if not source_id or source_id in supplied_sources:
                metadata_blockers.append("PERMISSION_SOURCE_SUMMARY_DUPLICATE_OR_MISSING_ID")
                continue
            supplied_sources[source_id] = source
        if set(supplied_sources) != set(expected_sources) or not expected_sources:
            metadata_blockers.append("PERMISSION_SOURCE_SET_MISMATCH")
        else:
            for source_id, summary in supplied_sources.items():
                raw_values = (summary.get("credibility"), expected_sources[source_id].get("credibility"),
                              summary.get("age_seconds"), expected_sources[source_id].get("age_seconds"))
                if any(isinstance(value, bool) for value in raw_values):
                    metadata_blockers.append("PERMISSION_SOURCE_SUMMARY_INVALID_NUMERIC_FIELDS")
                    continue
                try:
                    summary_credibility, original_credibility, summary_age, original_age = map(float, raw_values)
                except (TypeError, ValueError, OverflowError):
                    metadata_blockers.append("PERMISSION_SOURCE_SUMMARY_INVALID_NUMERIC_FIELDS")
                    continue
                if (not math.isfinite(summary_credibility) or not math.isfinite(original_credibility)
                        or not math.isfinite(summary_age) or not math.isfinite(original_age)
                        or abs(summary_credibility - original_credibility) > 1e-9
                        or abs(summary_age - original_age) > 1e-9):
                    metadata_blockers.append("PERMISSION_SOURCE_SUMMARY_MISMATCH")
    if metadata_blockers:
        emit("PERMISSION_REQUEST_BINDING", {"signal_id": signal_id, "status": "DENY",
                                             "blockers": sorted(set(metadata_blockers))})
        return finish("DENY", metadata_blockers, certainty=certainty, chief=chief,
                      message="Permission request does not match the reviewed signal; no order was created.")

    compliance_agent_id = str(certainty_policy.get("compliance_agent_id", "ComplianceAgent")).strip()
    compliance_rows = [v for v in verdicts if str(v.get("agent_id", "")).strip() == compliance_agent_id]
    compliance_status = (
        "PASS" if len(compliance_rows) == 1 and compliance_rows[0].get("verdict") == "APPROVE" else "BLOCKED"
    )
    emit("COMPLIANCE_VERDICT", {
        "signal_id": signal_id,
        "status": compliance_status,
        "compliance_agent_id": compliance_agent_id,
        "verdict_count": len(compliance_rows),
        "blockers": [] if compliance_status == "PASS" else ["COMPLIANCE_VERDICT_NOT_APPROVED"],
    })
    req["chief_verdict"] = chief.get("verdict")
    req["certainty_status"] = certainty.get("status")
    req["compliance_status"] = compliance_status
    req["risk_flags"] = all_risk_flags
    user_decision = bundle.get("user_decision")
    decision_envelope = (operator_decision_envelope if operator_decision_envelope is not None
                         else bundle.get("user_decision_envelope"))
    if user_decision is not None:
        # Approval is outside the evidence envelope on purpose: whoever can sign evidence must
        # not be able to approve a trade. The operator signature uses a distinct key and issuer.
        operator_config_blockers: list[str] = []
        operator_issuer = str(expected_operator_issuer or "").strip()
        if not operator_issuer:
            operator_config_blockers.append("OPERATOR_ISSUER_NOT_CONFIGURED")
        elif operator_issuer in {str(expected_evidence_issuer or "").strip(),
                                 str(expected_governance_issuer or "").strip()}:
            operator_config_blockers.append("OPERATOR_ISSUER_NOT_DISTINCT")
        try:
            operator_key_bytes = key_bytes(operator_key)
        except EvidenceError:
            operator_config_blockers.append("OPERATOR_KEY_NOT_CONFIGURED")
        else:
            try:
                if operator_key_bytes in (key_bytes(evidence_key), key_bytes(governance_key)):
                    operator_config_blockers.append("OPERATOR_KEY_NOT_DISTINCT")
            except EvidenceError:
                pass
        if operator_config_blockers:
            emit("OPERATOR_DECISION", {"signal_id": signal_id, "status": "DENY",
                                        "blockers": operator_config_blockers})
            return finish("DENY", operator_config_blockers, certainty=certainty, chief=chief)
        if not isinstance(user_decision, Mapping):
            emit("OPERATOR_DECISION", {"signal_id": signal_id, "status": "DENY",
                                        "blockers": ["USER_DECISION_INVALID"]})
            return finish("DENY", ["USER_DECISION_INVALID"], certainty=certainty, chief=chief)
        decision_check = verify_decision(
            user_decision, decision_envelope, key=operator_key,
            expected_issuer=operator_issuer, now=current,
        )
        emit("OPERATOR_DECISION", {
            "signal_id": signal_id,
            "status": "PASS" if decision_check.get("valid") is True else "DENY",
            "issuer": decision_check.get("issuer"),
            "payload_sha256": decision_check.get("payload_sha256"),
            "blockers": list(decision_check.get("blockers", [])),
        })
        if decision_check.get("valid") is not True:
            return finish("DENY", list(decision_check.get("blockers", [])),
                          certainty=certainty, chief=chief)
    if user_decision is not None and require_persisted_risk is not True:
        # A client-supplied approval must never bypass the persisted kill switch and
        # fresh terminal risk snapshot. Authorization therefore requires this path.
        emit("USER_AUTHORIZATION", {"signal_id": signal_id, "status": "DENY",
                                    "reason": "PERSISTED_RISK_REQUIRED_FOR_AUTHORIZATION"})
        return finish("DENY", ["PERSISTED_RISK_REQUIRED_FOR_AUTHORIZATION"],
                      certainty=certainty, chief=chief)
    if require_persisted_risk is True:
        permission = risk_controlled_permission(
            req, permission_matrix,
            kill_switch_state=kill_switch_state,
            snapshot=terminal_risk_snapshot,
            now=current,
        )
    else:
        permission = permission_gate(req, permission_matrix, now=current)
    emit("PERMISSION_GATE", {"signal_id": signal_id, "status": permission.get("status"),
                              "blockers": permission.get("blockers", []),
                              "permission_id": permission.get("permission_id"),
                              "persisted_risk_required": require_persisted_risk,
                              "kill_switch": permission.get("kill_switch"),
                              "risk_snapshot_validation": permission.get("risk_snapshot_validation")})
    if permission.get("status") == "DENY":
        return finish("DENY", list(permission.get("blockers", [])), certainty=certainty, chief=chief, permission=permission)

    if user_decision is not None:
        # The signed approval must name this signal, the exact Chief reasoning reviewed and
        # the exact order parameters; otherwise an evidence signer could resize the trade
        # after the human approved a different one.
        decision_binding_blockers: list[str] = []
        if str(user_decision.get("signal_id", "")).strip() != signal_id:
            decision_binding_blockers.append("USER_DECISION_SIGNAL_ID_MISMATCH")
        if user_decision.get("chief_reasoning_hash") != chief.get("reasoning_hash"):
            decision_binding_blockers.append("USER_DECISION_CHIEF_HASH_MISMATCH")
        try:
            approved_permission_hash = canonical_hash(dict(permission_request))
        except (TypeError, ValueError):
            approved_permission_hash = None
        if (approved_permission_hash is None
                or user_decision.get("permission_request_sha256") != approved_permission_hash):
            decision_binding_blockers.append("USER_DECISION_PERMISSION_HASH_MISMATCH")
        # The human must have approved the terminal snapshot the risk gate actually validated;
        # a decision signed against a different (or absent) snapshot is not this authorization.
        try:
            validated_snapshot_hash = (canonical_hash(dict(terminal_risk_snapshot))
                                       if isinstance(terminal_risk_snapshot, Mapping) else None)
        except (TypeError, ValueError):
            validated_snapshot_hash = None
        if (validated_snapshot_hash is None
                or user_decision.get("risk_snapshot_sha256") != validated_snapshot_hash):
            decision_binding_blockers.append("USER_DECISION_RISK_SNAPSHOT_MISMATCH")
        if decision_binding_blockers:
            emit("USER_AUTHORIZATION", {"signal_id": signal_id, "status": "DENY",
                                        "blockers": decision_binding_blockers})
            return finish("DENY", decision_binding_blockers, certainty=certainty, chief=chief,
                          permission=permission)

    # The human must approve the risk the persisted gate actually validated, not a
    # client-declared summary echoed back from the bundle.
    if require_persisted_risk is True and isinstance(terminal_risk_snapshot, Mapping):
        packet_risk_snapshot = dict(terminal_risk_snapshot)
        packet_risk_source = "persisted_terminal_snapshot"
    elif isinstance(req.get("risk_snapshot"), Mapping):
        packet_risk_snapshot = dict(req["risk_snapshot"])
        packet_risk_source = "client_supplied_unverified"
    else:
        packet_risk_snapshot = {}
        packet_risk_source = "missing"
    human_review_packet = {
        "signal_id": signal_id,
        "peer_reviews": [dict(review) for review in reviews],
        "dissenting_opinions": [dict(review) for review in challenge_reviews],
        "challenge_ledger": [dict(challenge) for challenge in challenge_records],
        "chief_reasoning": dict(chief_input),
        "tail_risk_probability": chief_input.get("tail_risk_probability"),
        "tail_risk_scenarios": chief_input.get("tail_risk_scenarios", []),
        "source_reports": [dict(source) for source in key_sources if isinstance(source, Mapping)],
        "risk_snapshot": packet_risk_snapshot,
        "risk_snapshot_source": packet_risk_source,
        "risk_snapshot_validation": permission.get("risk_snapshot_validation"),
        "risk_reconciliation": permission.get("risk_reconciliation"),
        "risk_flags": list(all_risk_flags),
        "permission_summary": {key: req.get(key) for key in (
            "instrument", "direction", "size", "expected_price", "sl_tp", "expected_rr", "consensus_score", "mode", "expires_at"
        )},
    }
    if user_decision is None:
        return finish("REQUIRE_HUMAN", [], certainty=certainty, chief=chief, permission=permission,
                      human_review_packet=human_review_packet,
                      message="Human review is required; no order command has been created.")
    try:
        order_scope = permission_tombstone_scope(req)
    except (TypeError, ValueError):
        emit("USER_AUTHORIZATION", {"signal_id": signal_id, "status": "DENY",
                                    "blockers": ["AUTHORIZATION_ORDER_SCOPE_INVALID"]})
        return finish("DENY", ["AUTHORIZATION_ORDER_SCOPE_INVALID"], certainty=certainty,
                      chief=chief, permission=permission)
    authorization = user_authorization_gate(
        permission, user_decision, now=current, nonce_store=authorization_store,
        order_scope=order_scope,
    )
    emit("USER_AUTHORIZATION", {"signal_id": signal_id, "status": authorization.get("status"),
                                 "permission_id": authorization.get("permission_id"),
                                 "order_scope": authorization.get("order_scope"),
                                 "blockers": authorization.get("blockers", [])})
    if authorization.get("authorized") is True:
        return finish("AUTHORIZED_ONCE", [], certainty=certainty, chief=chief, permission=permission,
                      authorization=authorization, human_review_packet=human_review_packet,
                      message="One-time permission metadata validated; no order was created or sent.")
    return finish("DENY", list(authorization.get("blockers", [])), certainty=certainty, chief=chief,
                  permission=permission, authorization=authorization, human_review_packet=human_review_packet)
