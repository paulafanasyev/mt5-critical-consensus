"""Deterministic orchestration of already-produced agent outputs.

No LLM, market-data ingestion, broker API, or order dispatch occurs here. This module
validates hand-offs and creates an in-memory hash-linked decision trace. Durable,
append-only persistence must be supplied by the deployment adapter.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from typing import Any, Mapping, Sequence

from .audit import canonical_hash, make_audit_event, verify_audit_chain
from .calibration import weighted_consensus
from .contracts import validate_contract
from .gates import certainty_gate, chief_skeptic_pass, permission_gate, user_authorization_gate

_VERDICT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERDICT_FUTURE_SKEW_SECONDS = 30
_VERDICT_MAX_AGE_SECONDS = 300
_CHIEF_MAX_AGE_SECONDS = 300
_REVIEW_MAX_AGE_SECONDS = 300
_REVIEW_FUTURE_SKEW_SECONDS = 30
_CHALLENGE_MAX_AGE_SECONDS = 86400
_CHALLENGE_FUTURE_SKEW_SECONDS = 30
_RESOLUTION_MAX_AGE_SECONDS = 300


def _as_records(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _nonempty_string_refs(value: Any) -> bool:
    return (isinstance(value, Sequence) and not isinstance(value, (str, bytes))
            and bool(value) and all(isinstance(item, str) and bool(item.strip()) for item in value))


def _parse_aware_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _validate_verdict_metadata(verdict: Mapping[str, Any], signal_id: str, current: datetime) -> list[str]:
    """Bind verdict metadata and apply a server-owned maximum age.

    `input_hash` is syntax-checked only: producer inputs are absent from this contract,
    so this hash does not prove provenance or integrity.
    """
    blockers: list[str] = []
    if not isinstance(verdict.get("verdict_id"), str) or not verdict["verdict_id"].strip():
        blockers.append("VERDICT_ID_MISSING")
    if not isinstance(verdict.get("agent_id"), str) or not verdict["agent_id"].strip():
        blockers.append("VERDICT_AGENT_ID_MISSING")
    raw_signal_id = verdict.get("signal_id")
    if not signal_id or not isinstance(raw_signal_id, str) or raw_signal_id.strip() != signal_id:
        blockers.append("VERDICT_SIGNAL_ID_MISMATCH")
    input_hash = verdict.get("input_hash")
    if not isinstance(input_hash, str) or _VERDICT_HASH_RE.fullmatch(input_hash) is None:
        blockers.append("VERDICT_INPUT_HASH_INVALID")
    raw_ttl = verdict.get("ttl_seconds")
    if isinstance(raw_ttl, bool) or not isinstance(raw_ttl, int) or not 1 <= raw_ttl <= _VERDICT_MAX_AGE_SECONDS:
        blockers.append("VERDICT_TTL_INVALID")
        ttl_seconds = None
    else:
        ttl_seconds = raw_ttl
    created_at = _parse_aware_time(verdict.get("created_at"))
    if created_at is None:
        blockers.append("VERDICT_CREATED_AT_MISSING_OR_INVALID")
    else:
        age_seconds = (current - created_at).total_seconds()
        if age_seconds < -_VERDICT_FUTURE_SKEW_SECONDS:
            blockers.append("VERDICT_CREATED_AT_IN_FUTURE")
        elif age_seconds > _VERDICT_MAX_AGE_SECONDS:
            blockers.append("VERDICT_EXPIRED")
        elif ttl_seconds is not None and age_seconds > ttl_seconds:
            blockers.append("VERDICT_EXPIRED")
    return blockers


def evaluate_signal_bundle(bundle: Mapping[str, Any], certainty_policy: Mapping[str, Any],
                           permission_matrix: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Evaluate certainty → ChiefAgent → Permission → user authorization, fail-closed.

    No OrderCommand is returned and ``trade_allowed`` is always false. Peer reviews
    must come from a trusted internal review service, not directly from bridge callers.
    """
    if not isinstance(bundle, Mapping):
        return {"status": "GATHER_MORE", "trade_allowed": False,
                "blockers": ["INVALID_SIGNAL_BUNDLE"], "audit_events": []}
    try:
        current = datetime.now(timezone.utc) if now is None else now
        if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("timezone-aware current time required")
        current = current.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return {"status": "GATHER_MORE", "trade_allowed": False,
                "blockers": ["INVALID_CURRENT_TIME"], "audit_events": []}

    trace: list[dict[str, Any]] = []
    previous_hash: str | None = None
    raw_signal_id = bundle.get("signal_id", "")
    signal_id = raw_signal_id.strip() if isinstance(raw_signal_id, str) else ""

    def emit(event_type: str, payload: Mapping[str, Any]) -> None:
        nonlocal previous_hash
        event = make_audit_event(event_type, payload, previous_hash)
        trace.append(event)
        previous_hash = event["event_hash"]

    def finish(status: str, blockers: list[str], **details: Any) -> dict[str, Any]:
        emit("PIPELINE_RESULT", {"signal_id": signal_id or "unknown", "status": status,
                                 "blockers": sorted(set(blockers)), "detail_hash": canonical_hash(details)})
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
    if (not isinstance(raw_verdicts, Sequence) or isinstance(raw_verdicts, (str, bytes))
            or len(verdicts) != len(raw_verdicts)):
        metadata_blockers.append("VERDICT_RECORD_INVALID")
    for index, verdict in enumerate(verdicts):
        valid, schema_blockers = validate_contract("agent_verdict_v2.schema.json", verdict)
        if not valid:
            metadata_blockers.append(f"VERDICT_{index}:VERDICT_SCHEMA_INVALID")
        metadata_blockers.extend(f"VERDICT_{index}:{code}"
                                 for code in _validate_verdict_metadata(verdict, signal_id, current))
    if metadata_blockers:
        emit("VERDICT_METADATA_VALIDATION", {"signal_id": signal_id or "unknown", "status": "FAILED",
                                               "blockers": sorted(set(metadata_blockers))})
        return finish("GATHER_MORE", metadata_blockers,
                      verdict_metadata_validation={"status": "FAILED", "blockers": sorted(set(metadata_blockers))})
    emit("VERDICT_METADATA_VALIDATION", {"signal_id": signal_id, "status": "PASSED",
                                           "verdict_count": len(verdicts)})

    raw_reviews = bundle.get("peer_reviews", [])
    reviews = _as_records(raw_reviews)
    review_blockers: list[str] = []
    if (not isinstance(raw_reviews, Sequence) or isinstance(raw_reviews, (str, bytes))
            or len(reviews) != len(raw_reviews)):
        review_blockers.append("PEER_REVIEW_SET_INVALID_OR_HAS_DROPPED_RECORDS")
    review_index: dict[str, set[str]] = {}
    review_ids: set[str] = set()
    challenge_blockers: list[str] = []
    challenge_reviews: list[Mapping[str, Any]] = []
    allowed_challenge_types = {"DATA_CONTRADICTION", "SOURCE_WEAK", "STALE_DATA", "LOGIC_GAP",
                               "REGIME_MISMATCH", "CORRELATION_RISK"}
    allowed_review_types = allowed_challenge_types | {"NONE"}
    for review in reviews:
        valid, _ = validate_contract("peer_review.schema.json", review)
        if not valid:
            review_blockers.append("INVALID_PEER_REVIEW_RECORD")
            continue
        review_id = review["review_id"].strip()
        if review_id in review_ids:
            review_blockers.append("DUPLICATE_PEER_REVIEW_ID")
            continue
        review_ids.add(review_id)
        target_id = review["target_verdict_id"].strip()
        reviewer_alias = review["reviewer_alias"].strip()
        stance = review["stance"]
        challenge_type = review["challenge_type"]
        if challenge_type not in allowed_review_types or (stance == "CHALLENGE" and challenge_type not in allowed_challenge_types):
            review_blockers.append("INVALID_PEER_REVIEW_RECORD")
            continue
        review_created = _parse_aware_time(review["created_at"])
        if review_created is None:
            review_blockers.append("PEER_REVIEW_TIMESTAMP_INVALID_OR_STALE")
            continue
        review_age = (current - review_created).total_seconds()
        if review_age > _REVIEW_MAX_AGE_SECONDS or review_age < -_REVIEW_FUTURE_SKEW_SECONDS:
            review_blockers.append("PEER_REVIEW_TIMESTAMP_INVALID_OR_STALE")
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
        verdict_id = verdict["verdict_id"].strip()
        if not verdict_id or verdict_id in verdict_ids:
            review_blockers.append("MISSING_OR_DUPLICATE_VERDICT_ID")
        if verdict_id:
            verdict_ids.add(verdict_id)
        verdict["reviewer_count"] = len(review_index.get(verdict_id, set()))
    if any(target_id not in verdict_ids for target_id in review_index):
        review_blockers.append("PEER_REVIEW_TARGET_NOT_FOUND")

    raw_challenges = bundle.get("challenge_records")
    if "challenge_records" not in bundle:
        challenge_blockers.append("CHALLENGE_LEDGER_MISSING")
        challenge_records: list[Mapping[str, Any]] = []
    elif not isinstance(raw_challenges, Sequence) or isinstance(raw_challenges, (str, bytes)):
        challenge_blockers.append("CHALLENGE_LEDGER_INVALID")
        challenge_records = []
    else:
        challenge_records = _as_records(raw_challenges)
        if len(challenge_records) != len(raw_challenges):
            challenge_blockers.append("CHALLENGE_LEDGER_HAS_INVALID_RECORDS")

    challenge_ids: set[str] = set()
    for challenge in challenge_records:
        valid, _ = validate_contract("challenge_record.schema.json", challenge)
        if not valid:
            challenge_blockers.append("CHALLENGE_SCHEMA_INVALID")
        challenge_id = challenge.get("challenge_id")
        if not isinstance(challenge_id, str) or not challenge_id.strip() or challenge_id.strip() in challenge_ids:
            challenge_blockers.append("CHALLENGE_ID_MISSING_OR_DUPLICATE")
        else:
            challenge_ids.add(challenge_id.strip())
        target_id = challenge.get("target_verdict_id")
        if not isinstance(target_id, str) or not target_id.strip() or target_id.strip() not in verdict_ids:
            challenge_blockers.append("CHALLENGE_TARGET_VERDICT_INVALID")
        if challenge.get("challenge_type") not in allowed_challenge_types:
            challenge_blockers.append("CHALLENGE_TYPE_INVALID")
        status_value = challenge.get("status")
        if status_value not in {"OPEN", "RESOLVED", "ESCALATED"}:
            challenge_blockers.append("CHALLENGE_STATUS_INVALID")
        created = _parse_aware_time(challenge.get("created_at"))
        if created is None:
            challenge_blockers.append("CHALLENGE_CREATED_AT_INVALID")
        else:
            age = (current - created).total_seconds()
            if age > _CHALLENGE_MAX_AGE_SECONDS or age < -_CHALLENGE_FUTURE_SKEW_SECONDS:
                challenge_blockers.append("CHALLENGE_CREATED_AT_STALE_OR_FUTURE")
        rounds = challenge.get("rounds")
        if not isinstance(rounds, Sequence) or isinstance(rounds, (str, bytes)) or len(rounds) > 3 or any(not isinstance(r, Mapping) for r in rounds):
            challenge_blockers.append("CHALLENGE_ROUNDS_INVALID")
        if status_value == "RESOLVED":
            resolved_at = _parse_aware_time(challenge.get("resolved_at"))
            if resolved_at is None:
                challenge_blockers.append("CHALLENGE_RESOLVED_AT_INVALID")
            else:
                resolution_age = (current - resolved_at).total_seconds()
                if resolution_age > _RESOLUTION_MAX_AGE_SECONDS or resolution_age < -_CHALLENGE_FUTURE_SKEW_SECONDS:
                    challenge_blockers.append("CHALLENGE_RESOLUTION_STALE_OR_FUTURE")
            if not _nonempty_string_refs(challenge.get("resolution_evidence_refs")):
                challenge_blockers.append("CHALLENGE_RESOLUTION_MISSING_OR_INVALID_EVIDENCE")

    registered_challenges = {(ch.get("target_verdict_id"), ch.get("challenge_type")) for ch in challenge_records}
    for review in challenge_reviews:
        key = (review.get("target_verdict_id"), review.get("challenge_type"))
        if key not in registered_challenges:
            review_blockers.append("PEER_CHALLENGE_NOT_REGISTERED_IN_CHALLENGE_LEDGER")

    open_challenges: list[Mapping[str, Any]] = []
    for challenge in challenge_records:
        if challenge.get("status") != "RESOLVED" or not _nonempty_string_refs(challenge.get("resolution_evidence_refs")):
            open_challenges.append(dict(challenge) if challenge.get("status") != "RESOLVED"
                                    else {**dict(challenge), "status": "OPEN"})
    supplied_open = bundle.get("open_challenges", [])
    if isinstance(supplied_open, Sequence) and not isinstance(supplied_open, (str, bytes)):
        known = {c.get("challenge_id") for c in open_challenges if isinstance(c, Mapping)}
        for challenge in supplied_open:
            if not isinstance(challenge, Mapping):
                challenge_blockers.append("OPEN_CHALLENGE_RECORD_INVALID")
                continue
            challenge_id = challenge.get("challenge_id")
            if not isinstance(challenge_id, str) or not challenge_id.strip():
                challenge_blockers.append("OPEN_CHALLENGE_ID_INVALID")
            elif challenge_id not in known:
                open_challenges.append(challenge)
                known.add(challenge_id)
    elif "open_challenges" in bundle:
        challenge_blockers.append("INVALID_OPEN_CHALLENGE_STATE")

    raw_resolution_status = bundle.get("challenge_resolution_status", "")
    allowed_resolution_statuses = {"DEADLOCK", "OPEN", "RESOLVED", "NONE"}
    if not isinstance(raw_resolution_status, str) or raw_resolution_status not in allowed_resolution_statuses:
        challenge_blockers.append("CHALLENGE_RESOLUTION_STATUS_INVALID")
        resolution_status = "INVALID"
    else:
        resolution_status = raw_resolution_status
    computed_resolution_status = "OPEN" if open_challenges else "RESOLVED" if challenge_records else "NONE"
    if resolution_status == "DEADLOCK":
        pass
    elif resolution_status != "INVALID" and resolution_status != computed_resolution_status:
        challenge_blockers.append("CHALLENGE_RESOLUTION_STATUS_MISMATCH")
        resolution_status = computed_resolution_status
    elif resolution_status == "INVALID":
        resolution_status = "OPEN" if open_challenges else "MISSING"

    risk_flags = bundle.get("risk_flags", [])
    if not isinstance(risk_flags, Sequence) or isinstance(risk_flags, (str, bytes)):
        risk_flags = ["INVALID_RISK_FLAGS"]
    all_risk_flags = [str(x) for x in risk_flags if str(x).strip()]
    all_risk_flags.extend(review_blockers)
    all_risk_flags.extend(challenge_blockers)

    raw_sources = bundle.get("key_sources", [])
    if not isinstance(raw_sources, Sequence) or isinstance(raw_sources, (str, bytes)):
        all_risk_flags.append("KEY_SOURCE_SET_INVALID")
        sources_for_certainty = []
    else:
        sources_for_certainty = list(raw_sources)
        source_ids = [s.get("source_id") for s in sources_for_certainty if isinstance(s, Mapping)]
        valid_source_ids = [sid.strip() for sid in source_ids if isinstance(sid, str) and sid.strip()]
        if len(valid_source_ids) != len(source_ids) or len(valid_source_ids) != len(set(valid_source_ids)):
            all_risk_flags.append("KEY_SOURCE_IDS_INVALID_OR_DUPLICATE")

    try:
        consensus_score = weighted_consensus(verdicts)
    except (TypeError, ValueError, KeyError, OverflowError):
        consensus_score = 0.0
        all_risk_flags.append("CALIBRATED_CONSENSUS_COULD_NOT_BE_COMPUTED")

    certainty_input = {
        "verdicts": verdicts, "consensus_score": consensus_score,
        "open_challenges": open_challenges, "challenge_resolution_status": resolution_status,
        "contradictions_last_n_hours": bundle.get("contradictions_last_n_hours"),
        "key_sources": sources_for_certainty, "news_or_rumor": bundle.get("news_or_rumor", False),
        "devil_advocate_report": bundle.get("devil_advocate_report"),
        "dissent_rate": bundle.get("dissent_rate"), "dissent_sample_size": bundle.get("dissent_sample_size"),
        "false_certainty_rate": bundle.get("false_certainty_rate"),
        "false_certainty_sample_size": bundle.get("false_certainty_sample_size"), "risk_flags": all_risk_flags,
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
    chief_valid, _ = validate_contract("chief_reasoning.schema.json", chief_input)
    if not chief_valid:
        return finish("HOLD", ["CHIEF_REASONING_SCHEMA_INVALID"], certainty=certainty)
    chief_signal = chief_input.get("signal_id")
    if not isinstance(chief_signal, str) or chief_signal.strip() != signal_id:
        return finish("HOLD", ["CHIEF_SIGNAL_ID_MISMATCH"], certainty=certainty)
    chief_created = _parse_aware_time(chief_input.get("created_at"))
    if chief_created is None:
        return finish("HOLD", ["CHIEF_CREATED_AT_INVALID"], certainty=certainty)
    chief_age = (current - chief_created).total_seconds()
    if chief_age > _CHIEF_MAX_AGE_SECONDS or chief_age < -_VERDICT_FUTURE_SKEW_SECONDS:
        return finish("HOLD", ["CHIEF_REASONING_STALE_OR_FUTURE"], certainty=certainty,
                      chief_age_seconds=chief_age)
    actual_challenge_snapshot = sorted((dict(ch) for ch in challenge_records),
                                       key=lambda row: str(row.get("challenge_id", "")))
    if canonical_hash(chief_input.get("challenge_ledger_snapshot", [])) != canonical_hash(actual_challenge_snapshot):
        return finish("HOLD", ["CHIEF_CHALLENGE_LEDGER_SNAPSHOT_MISMATCH"], certainty=certainty)
    supplied_hash = chief_input.get("reasoning_hash")
    if not isinstance(supplied_hash, str) or not supplied_hash.strip():
        return finish("HOLD", ["CHIEF_REASONING_HASH_MISSING"], certainty=certainty)
    unsigned_chief = dict(chief_input); unsigned_chief.pop("reasoning_hash", None)
    expected_hash = canonical_hash(unsigned_chief)
    if supplied_hash != expected_hash:
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
        return finish("DENY", ["PERMISSION_REQUEST_MISSING"], certainty=certainty, chief=chief)
    req = dict(permission_request)
    metadata_blockers: list[str] = []
    permission_id = req.get("permission_id")
    if not isinstance(permission_id, str) or not permission_id.strip():
        metadata_blockers.append("PERMISSION_ID_INVALID")
    permission_signal = req.get("signal_id")
    if not isinstance(permission_signal, str) or permission_signal.strip() != signal_id:
        metadata_blockers.append("PERMISSION_SIGNAL_ID_MISMATCH")
    if req.get("chief_reasoning_hash") != chief.get("reasoning_hash"):
        metadata_blockers.append("PERMISSION_CHIEF_REASONING_HASH_MISMATCH")
    try:
        raw_score = req.get("consensus_score")
        permission_score = float(raw_score) if not isinstance(raw_score, bool) else float("nan")
    except (TypeError, ValueError, OverflowError):
        permission_score = float("nan")
    if not math.isfinite(permission_score) or abs(permission_score - consensus_score) > 1e-9:
        metadata_blockers.append("PERMISSION_CONSENSUS_SCORE_MISMATCH")
    source_rows = req.get("sources")
    if not isinstance(source_rows, Sequence) or isinstance(source_rows, (str, bytes)):
        metadata_blockers.append("PERMISSION_SOURCE_SUMMARY_MISSING_OR_INVALID")
    else:
        expected_sources = {s["source_id"].strip(): s for s in sources_for_certainty if isinstance(s, Mapping)
                            and isinstance(s.get("source_id"), str) and s["source_id"].strip()}
        supplied_sources: dict[str, Mapping[str, Any]] = {}
        for source in source_rows:
            if not isinstance(source, Mapping):
                metadata_blockers.append("PERMISSION_SOURCE_SUMMARY_INVALID"); continue
            raw_id = source.get("id", "")
            source_id = raw_id.strip() if isinstance(raw_id, str) else ""
            if not source_id or source_id in supplied_sources:
                metadata_blockers.append("PERMISSION_SOURCE_SUMMARY_DUPLICATE_OR_MISSING_ID"); continue
            supplied_sources[source_id] = source
        if set(supplied_sources) != set(expected_sources) or not expected_sources:
            metadata_blockers.append("PERMISSION_SOURCE_SET_MISMATCH")
        else:
            for source_id, summary in supplied_sources.items():
                values = (summary.get("credibility"), expected_sources[source_id].get("credibility"),
                          summary.get("age_seconds"), expected_sources[source_id].get("age_seconds"))
                if any(isinstance(value, bool) for value in values):
                    metadata_blockers.append("PERMISSION_SOURCE_SUMMARY_INVALID_NUMERIC_FIELDS"); continue
                try:
                    sc, oc, sa, oa = map(float, values)
                except (TypeError, ValueError, OverflowError):
                    metadata_blockers.append("PERMISSION_SOURCE_SUMMARY_INVALID_NUMERIC_FIELDS"); continue
                if (not all(map(math.isfinite, (sc, oc, sa, oa))) or abs(sc - oc) > 1e-9 or abs(sa - oa) > 1e-9):
                    metadata_blockers.append("PERMISSION_SOURCE_SUMMARY_MISMATCH")
    req["chief_verdict"] = chief.get("verdict")
    req["certainty_status"] = certainty.get("status")
    req["compliance_status"] = bundle.get("compliance_status")
    req["risk_flags"] = all_risk_flags
    permission_valid, _ = validate_contract("permission_request.schema.json", req)
    if not permission_valid:
        metadata_blockers.append("PERMISSION_REQUEST_SCHEMA_INVALID")
    if metadata_blockers:
        emit("PERMISSION_REQUEST_BINDING", {"signal_id": signal_id, "status": "DENY",
                                             "blockers": sorted(set(metadata_blockers))})
        return finish("DENY", metadata_blockers, certainty=certainty, chief=chief,
                      message="Permission request does not match the reviewed signal; no order was created.")
    permission = permission_gate(req, permission_matrix, now=current)
    emit("PERMISSION_GATE", {"signal_id": signal_id, "status": permission.get("status"),
                              "blockers": permission.get("blockers", []), "permission_id": permission.get("permission_id")})
    if permission.get("status") == "DENY":
        return finish("DENY", list(permission.get("blockers", [])), certainty=certainty, chief=chief,
                      permission=permission)

    packet = {"signal_id": signal_id, "peer_reviews": [dict(r) for r in reviews],
              "dissenting_opinions": [dict(r) for r in challenge_reviews],
              "challenge_ledger": [dict(c) for c in challenge_records],
              "chief_reasoning": dict(chief_input), "tail_risk_probability": chief_input.get("tail_risk_probability"),
              "tail_risk_scenarios": chief_input.get("tail_risk_scenarios", []),
              "source_reports": [dict(s) for s in sources_for_certainty if isinstance(s, Mapping)],
              "risk_snapshot": dict(req.get("risk_snapshot", {})) if isinstance(req.get("risk_snapshot"), Mapping) else {},
              "risk_flags": list(all_risk_flags),
              "permission_summary": {k: req.get(k) for k in ("instrument", "direction", "size", "expected_price",
                                     "sl_tp", "expected_rr", "consensus_score", "mode", "expires_at")}}
    user_decision = bundle.get("user_decision")
    if user_decision is None:
        return finish("REQUIRE_HUMAN", [], certainty=certainty, chief=chief, permission=permission,
                      human_review_packet=packet,
                      message="Human review is required; no order command has been created.")
    decision_valid, _ = validate_contract("user_decision.schema.json", user_decision)
    if not decision_valid:
        return finish("DENY", ["USER_DECISION_SCHEMA_INVALID"], certainty=certainty, chief=chief,
                      permission=permission, human_review_packet=packet)
    authorization = user_authorization_gate(permission, user_decision, now=current)
    emit("USER_AUTHORIZATION", {"signal_id": signal_id, "status": authorization.get("status"),
                                 "permission_id": authorization.get("permission_id"),
                                 "blockers": authorization.get("blockers", [])})
    return finish("DENY", list(authorization.get("blockers", [])), certainty=certainty,
                  chief=chief, permission=permission, authorization=authorization, human_review_packet=packet,
                  message="Approval metadata alone is insufficient: durable replay protection and server-side authorization state are not implemented.")
