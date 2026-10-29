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
from .gates import certainty_gate, chief_skeptic_pass, permission_gate, user_authorization_gate

_VERDICT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERDICT_FUTURE_SKEW_SECONDS = 30


def _as_records(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


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


def evaluate_signal_bundle(
    bundle: Mapping[str, Any],
    certainty_policy: Mapping[str, Any],
    permission_matrix: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate certainty → ChiefAgent → Permission → user authorization, fail-closed.

    The function returns no OrderCommand and always sets ``trade_allowed`` to false.
    Peer-review inputs are expected to come from the trusted internal review service;
    the bridge API must not accept user-supplied review records as authoritative evidence.
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

    trace: list[dict[str, Any]] = []
    previous_hash: str | None = None
    signal_id = str(bundle.get("signal_id", "")).strip()

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

    emit("SIGNAL_RECEIVED", {"signal_id": signal_id or "unknown", "bundle_hash": canonical_hash(bundle)})

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
    if metadata_blockers:
        emit("VERDICT_METADATA_VALIDATION", {"signal_id": signal_id or "unknown", "status": "FAILED",
                                               "blockers": sorted(set(metadata_blockers))})
        return finish("GATHER_MORE", metadata_blockers,
                      verdict_metadata_validation={"status": "FAILED", "blockers": sorted(set(metadata_blockers))})
    emit("VERDICT_METADATA_VALIDATION", {"signal_id": signal_id, "status": "PASSED",
                                           "verdict_count": len(verdicts)})

    reviews = _as_records(bundle.get("peer_reviews"))
    review_index: dict[str, set[str]] = {}
    review_blockers: list[str] = []
    challenge_blockers: list[str] = []
    challenge_reviews: list[Mapping[str, Any]] = []
    for review in reviews:
        target_id = str(review.get("target_verdict_id", "")).strip()
        reviewer_alias = str(review.get("reviewer_alias", "")).strip()
        stance = str(review.get("stance", "")).upper()
        evidence_refs = review.get("evidence_refs")
        evidence_ok = isinstance(evidence_refs, Sequence) and not isinstance(evidence_refs, (str, bytes)) and bool(evidence_refs) and all(str(x).strip() for x in evidence_refs)
        argument = str(review.get("argument", "")).strip()
        challenge_type = str(review.get("challenge_type", "")).upper()
        valid_challenge_types = {"DATA_CONTRADICTION", "SOURCE_WEAK", "STALE_DATA", "LOGIC_GAP", "REGIME_MISMATCH", "CORRELATION_RISK"}
        challenge_type_ok = stance != "CHALLENGE" or challenge_type in valid_challenge_types
        if review.get("status") != "VALIDATED" or not target_id or not reviewer_alias or stance not in {"CONFIRM", "CHALLENGE"} or not evidence_ok or not argument or not challenge_type_ok:
            review_blockers.append("INVALID_PEER_REVIEW_RECORD")
            continue
        target_set = review_index.setdefault(target_id, set())
        if reviewer_alias in target_set:
            review_blockers.append("DUPLICATE_PEER_REVIEWER_FOR_VERDICT")
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
    registered_challenges = {(str(ch.get("target_verdict_id", "")), str(ch.get("challenge_type", "")).upper()) for ch in challenge_records}
    for review in challenge_reviews:
        key = (str(review.get("target_verdict_id", "")), str(review.get("challenge_type", "")).upper())
        if key not in registered_challenges:
            review_blockers.append("PEER_CHALLENGE_NOT_REGISTERED_IN_CHALLENGE_LEDGER")

    open_challenges: list[Mapping[str, Any]] = []
    for challenge in challenge_records:
        status_value = str(challenge.get("status", "OPEN")).upper()
        if status_value == "RESOLVED":
            resolution_refs = challenge.get("resolution_evidence_refs", challenge.get("evidence_refs", []))
            if not isinstance(resolution_refs, Sequence) or isinstance(resolution_refs, (str, bytes)) or not resolution_refs:
                challenge_blockers.append("CHALLENGE_RESOLUTION_MISSING_EVIDENCE")
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

    certainty_input = {
        "verdicts": verdicts,
        "consensus_score": consensus_score,
        "open_challenges": open_challenges,
        "challenge_resolution_status": resolution_status,
        "contradictions_last_n_hours": bundle.get("contradictions_last_n_hours"),
        "key_sources": bundle.get("key_sources", []),
        "news_or_rumor": bundle.get("news_or_rumor", False),
        "devil_advocate_report": bundle.get("devil_advocate_report"),
        "dissent_rate": bundle.get("dissent_rate"),
        "dissent_sample_size": bundle.get("dissent_sample_size"),
        "false_certainty_rate": bundle.get("false_certainty_rate"),
        "false_certainty_sample_size": bundle.get("false_certainty_sample_size"),
        "risk_flags": all_risk_flags,
    }
    certainty = certainty_gate(certainty_input, certainty_policy)
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
    if canonical_hash(chief_input.get("challenge_ledger_snapshot", [])) != canonical_hash(actual_challenge_snapshot):
        emit("CHIEF_REVIEW", {"signal_id": signal_id, "status": "HOLD", "reason": "CHIEF_CHALLENGE_LEDGER_SNAPSHOT_MISMATCH"})
        return finish("HOLD", ["CHIEF_CHALLENGE_LEDGER_SNAPSHOT_MISMATCH"], certainty=certainty)
    supplied_reasoning_hash = chief_input.get("reasoning_hash")
    unsigned_chief = dict(chief_input)
    unsigned_chief.pop("reasoning_hash", None)
    expected_hash = canonical_hash(unsigned_chief)
    if supplied_reasoning_hash is None:
        chief_input["reasoning_hash"] = expected_hash
    elif supplied_reasoning_hash != expected_hash:
        emit("CHIEF_REVIEW", {"signal_id": signal_id, "status": "HOLD", "reason": "CHIEF_REASONING_HASH_MISMATCH"})
        return finish("HOLD", ["CHIEF_REASONING_HASH_MISMATCH"], certainty=certainty)
    chief = chief_skeptic_pass(chief_input, now=current)
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

    req["chief_verdict"] = chief.get("verdict")
    req["certainty_status"] = certainty.get("status")
    req["compliance_status"] = bundle.get("compliance_status")
    req["risk_flags"] = all_risk_flags
    permission = permission_gate(req, permission_matrix, now=current)
    emit("PERMISSION_GATE", {"signal_id": signal_id, "status": permission.get("status"),
                              "blockers": permission.get("blockers", []), "permission_id": permission.get("permission_id")})
    if permission.get("status") == "DENY":
        return finish("DENY", list(permission.get("blockers", [])), certainty=certainty, chief=chief, permission=permission)

    human_review_packet = {
        "signal_id": signal_id,
        "peer_reviews": [dict(review) for review in reviews],
        "dissenting_opinions": [dict(review) for review in challenge_reviews],
        "challenge_ledger": [dict(challenge) for challenge in challenge_records],
        "chief_reasoning": dict(chief_input),
        "tail_risk_probability": chief_input.get("tail_risk_probability"),
        "tail_risk_scenarios": chief_input.get("tail_risk_scenarios", []),
        "source_reports": [dict(source) for source in key_sources if isinstance(source, Mapping)],
        "risk_snapshot": dict(req.get("risk_snapshot", {})) if isinstance(req.get("risk_snapshot"), Mapping) else {},
        "risk_flags": list(all_risk_flags),
        "permission_summary": {key: req.get(key) for key in (
            "instrument", "direction", "size", "expected_price", "sl_tp", "expected_rr", "consensus_score", "mode", "expires_at"
        )},
    }
    user_decision = bundle.get("user_decision")
    if user_decision is None:
        return finish("REQUIRE_HUMAN", [], certainty=certainty, chief=chief, permission=permission,
                      human_review_packet=human_review_packet,
                      message="Human review is required; no order command has been created.")
    authorization = user_authorization_gate(permission, user_decision, now=current)
    emit("USER_AUTHORIZATION", {"signal_id": signal_id, "status": authorization.get("status"),
                                 "permission_id": authorization.get("permission_id"),
                                 "blockers": authorization.get("blockers", [])})
    if authorization.get("authorized") is True:
        return finish("AUTHORIZED_ONCE", [], certainty=certainty, chief=chief, permission=permission,
                      authorization=authorization, human_review_packet=human_review_packet,
                      message="One-time permission metadata validated; no order was created or sent.")
    return finish("DENY", list(authorization.get("blockers", [])), certainty=certainty, chief=chief,
                  permission=permission, authorization=authorization, human_review_packet=human_review_packet)
