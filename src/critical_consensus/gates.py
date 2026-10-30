"""Fail-closed policy gates. Functions return decision records; they never execute trades."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import math
import threading
import time
import re
import uuid
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .audit import canonical_hash


# Daemon workers bound resolver concurrency and cannot hold process shutdown open.
# A running Python callback cannot be forcibly stopped, so timed-out workers keep
# their slot until completion; this caps abandoned callbacks at four per process.
_CHALLENGE_RESOLVER_SLOTS = threading.BoundedSemaphore(4)


def _run_bounded_challenge_resolver(resolver, challenge, round_no, timeout_seconds):
    if not _CHALLENGE_RESOLVER_SLOTS.acquire(timeout=timeout_seconds):
        return None, "RESOLVER_CAPACITY_EXHAUSTED"
    completed = threading.Event()
    outcome = {}

    def invoke():
        try:
            outcome["value"] = resolver(dict(challenge), round_no)
        except Exception as exc:
            outcome["error"] = type(exc).__name__
        finally:
            _CHALLENGE_RESOLVER_SLOTS.release()
            completed.set()

    worker = threading.Thread(target=invoke, name="cc-challenge-resolver", daemon=True)
    try:
        worker.start()
    except Exception as exc:
        _CHALLENGE_RESOLVER_SLOTS.release()
        return None, f"RESOLVER_START_FAILED:{type(exc).__name__}"
    if not completed.wait(timeout_seconds):
        return None, "RESOLVER_TIMEOUT"
    if "error" in outcome:
        return None, f"RESOLVER_FAILED:{outcome['error']}"
    return outcome.get("value"), None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _finite_float(value: Any) -> float | None:
    """Return a finite float or None; NaN/inf must never pass a policy comparison."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _current_utc(now: datetime | None) -> datetime:
    if now is None:
        return _now_utc()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("current time must include a timezone")
    return now.astimezone(timezone.utc)


def _parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp must contain timezone")
    return dt.astimezone(timezone.utc)


def self_adversarial_pass(verdict: Mapping[str, Any], *, counterarguments: Sequence[str],
                          contradictions: Sequence[Mapping[str, Any]],
                          sources: Sequence[Mapping[str, Any]], now: datetime | None = None,
                          required_independent_sources: int = 2,
                          news_or_rumor: bool = False) -> dict[str, Any]:
    """Validate self-critique, source freshness, independent evidence and MNPI flags."""
    if not isinstance(verdict, Mapping):
        verdict = {}
    reasons: list[str] = []
    if not isinstance(counterarguments, Sequence) or isinstance(counterarguments, (str, bytes)) or not 3 <= len(counterarguments) <= 5 or any(not str(x).strip() for x in counterarguments):
        reasons.append("SELF_ADVERSARIAL_REQUIRES_3_TO_5_CONCRETE_COUNTERARGUMENTS")
    if contradictions:
        reasons.append("UNRESOLVED_CONTRADICTIONS_PRESENT")
    try:
        current = _current_utc(now)
    except ValueError:
        current = _now_utc()
        reasons.append("INVALID_CURRENT_TIME")

    valid_sources: list[Mapping[str, Any]] = []
    invalid_source_seen = False
    for source in sources:
        if not isinstance(source, Mapping):
            invalid_source_seen = True
            continue
        credibility = _finite_float(source.get("credibility"))
        min_credibility = _finite_float(source.get("min_credibility", 0.75))
        age = _finite_float(source.get("age_seconds"))
        ttl = _finite_float(source.get("ttl_seconds"))
        if (not source.get("source_id") or credibility is None or min_credibility is None or age is None or ttl is None
                or age < 0 or ttl <= 0 or credibility < min_credibility or age > ttl):
            invalid_source_seen = True
            continue
        valid_sources.append(source)
    if invalid_source_seen or not valid_sources:
        reasons.append("SOURCE_STALE_LOW_CREDIBILITY_OR_INVALID")

    if news_or_rumor and len({s.get("independence_group", s.get("source_id")) for s in valid_sources}) < required_independent_sources:
        reasons.append("INSUFFICIENT_INDEPENDENT_FRESH_SOURCES")
    raw_confidence = _finite_float(verdict.get("confidence", 0.0))
    confidence = raw_confidence if raw_confidence is not None and 0 <= raw_confidence <= 1 else 0.0
    if raw_confidence is None or not 0 <= raw_confidence <= 1:
        reasons.append("INVALID_CONFIDENCE")
    supplied_verdict = str(verdict.get("verdict", "ABSTAIN")).upper()
    if supplied_verdict not in {"APPROVE", "ABSTAIN", "REJECT", "VETO"}:
        reasons.append("INVALID_VERDICT")
        normalized = "ABSTAIN"
    elif supplied_verdict == "APPROVE" and raw_confidence is not None and raw_confidence < 0.50:
        reasons.append("CONFIDENCE_BELOW_REJECTION_FLOOR")
        normalized = "REJECT"
    elif supplied_verdict == "APPROVE" and raw_confidence is not None and raw_confidence < 0.75:
        reasons.append("CONFIDENCE_BELOW_APPROVAL_FLOOR")
        normalized = "ABSTAIN"
    elif supplied_verdict == "APPROVE" and reasons:
        normalized = "ABSTAIN" if not contradictions else "REJECT"
    else:
        normalized = supplied_verdict
    if any(str(s.get("legal_tag", "")).upper() == "MNPI-SUSPECT" for s in sources if isinstance(s, Mapping)):
        normalized = "VETO"
        reasons.append("MNPI_SUSPECT_SOURCE_BLOCKED")
    return {
        "verdict_id": verdict.get("verdict_id"),
        "verdict": normalized,
        "confidence": confidence,
        "self_adversarial_passed": not reasons,
        "counterarguments": list(counterarguments) if not isinstance(counterarguments, (str, bytes)) else [],
        "blockers": sorted(set(reasons)),
        "validated_source_ids": [s.get("source_id") for s in valid_sources],
        "checked_at": current.isoformat(),
    }


async def anonymized_peer_review(
    verdicts: Sequence[Mapping[str, Any]],
    reviewers: Sequence[tuple[str, Callable[[dict[str, Any], list[dict[str, Any]]], Awaitable[dict[str, Any]]]]],
    *, min_reviewers_per_verdict: int = 2, review_timeout_seconds: float = 30.0,
    overall_timeout_seconds: float = 120.0,
) -> list[dict[str, Any]]:
    """Obtain anonymized peer reviews and reject malformed or evidence-free reviews."""
    output: list[dict[str, Any]] = []
    if (isinstance(min_reviewers_per_verdict, bool)
            or not isinstance(min_reviewers_per_verdict, int)
            or min_reviewers_per_verdict < 1):
        raise ValueError("min_reviewers_per_verdict must be a positive integer")
    if (isinstance(review_timeout_seconds, bool) or not isinstance(review_timeout_seconds, (int, float))
            or not math.isfinite(review_timeout_seconds) or review_timeout_seconds <= 0
            or review_timeout_seconds > 120):
        raise ValueError("review_timeout_seconds must be finite and in (0,120]")
    if (not isinstance(overall_timeout_seconds, (int, float))
            or isinstance(overall_timeout_seconds, bool)
            or not math.isfinite(overall_timeout_seconds)
            or overall_timeout_seconds <= 0 or overall_timeout_seconds > 600):
        raise ValueError("overall_timeout_seconds must be finite and in (0,600]")
    review_deadline = time.monotonic() + float(overall_timeout_seconds)
    if not verdicts or any(not isinstance(v, Mapping) for v in verdicts):
        return [{"status": "BLOCKED", "reason": "INVALID_OR_EMPTY_VERDICTS"}]
    if any(not isinstance(item, tuple) or len(item) != 2 or not str(item[0]).strip() or not callable(item[1]) for item in reviewers):
        return [{"status": "BLOCKED", "reason": "INVALID_REVIEWER_REGISTRY"}]
    reviewer_ids = [str(name) for name, _ in reviewers]
    if len(reviewer_ids) != len(set(reviewer_ids)):
        return [{"status": "BLOCKED", "reason": "DUPLICATE_REVIEWER_IDENTITIES"}]
    public = [{k: v.get(k) for k in ("verdict_id", "verdict", "confidence", "reasoning_summary", "evidence_refs")}
              for v in verdicts]
    for target_idx, target in enumerate(public):
        target_agent_id = str(verdicts[target_idx].get("agent_id", ""))
        possible = [(name, fn) for name, fn in reviewers if name != target_agent_id]
        if len(possible) < min_reviewers_per_verdict:
            output.append({"target_verdict_id": target.get("verdict_id"), "status": "BLOCKED",
                           "reason": "INSUFFICIENT_INDEPENDENT_REVIEWERS"})
            continue
        peers = [dict(v) for j, v in enumerate(public) if j != target_idx]
        for i, (_private_reviewer_id, review_fn) in enumerate(possible[:min_reviewers_per_verdict], start=1):
            remaining = review_deadline - time.monotonic()
            if remaining <= 0:
                output.append({"target_verdict_id": target.get("verdict_id"), "status": "BLOCKED",
                               "reason": "REVIEW_CYCLE_TIMEOUT", "reviewer_alias": f"reviewer-{i:02d}"})
                break
            overall_limited = remaining < float(review_timeout_seconds)
            try:
                review = await asyncio.wait_for(
                    review_fn(dict(target), [dict(x) for x in peers]),
                    timeout=min(float(review_timeout_seconds), remaining),
                )
            except asyncio.TimeoutError:
                output.append({"target_verdict_id": target.get("verdict_id"), "status": "BLOCKED",
                               "reason": "REVIEW_CYCLE_TIMEOUT" if overall_limited else "REVIEWER_TIMEOUT",
                               "reviewer_alias": f"reviewer-{i:02d}"})
                if overall_limited:
                    break
                continue
            except Exception as exc:  # fail closed; do not include potentially sensitive exception text
                output.append({"target_verdict_id": target.get("verdict_id"), "status": "BLOCKED",
                               "reason": "REVIEWER_CALLBACK_FAILED", "reviewer_alias": f"reviewer-{i:02d}"})
                continue
            if not isinstance(review, Mapping):
                output.append({"target_verdict_id": target.get("verdict_id"), "status": "BLOCKED",
                               "reason": "INVALID_REVIEW_OBJECT", "reviewer_alias": f"reviewer-{i:02d}"})
                continue
            stance = str(review.get("stance", "")).upper()
            challenge_type = str(review.get("challenge_type", "")).upper()
            argument = str(review.get("argument", "")).strip()
            refs = review.get("evidence_refs")
            refs_valid = isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)) and bool(refs) and all(str(x).strip() for x in refs)
            valid_challenge_types = {"DATA_CONTRADICTION", "SOURCE_WEAK", "STALE_DATA", "LOGIC_GAP", "REGIME_MISMATCH", "CORRELATION_RISK"}
            if stance not in {"CONFIRM", "CHALLENGE"} or not argument or not refs_valid:
                output.append({"target_verdict_id": target.get("verdict_id"), "status": "BLOCKED",
                               "reason": "REVIEW_REQUIRES_VALID_STANCE_ARGUMENT_AND_EVIDENCE",
                               "reviewer_alias": f"reviewer-{i:02d}"})
                continue
            if stance == "CHALLENGE" and challenge_type not in valid_challenge_types:
                output.append({"target_verdict_id": target.get("verdict_id"), "status": "BLOCKED",
                               "reason": "CHALLENGE_TYPE_INVALID", "reviewer_alias": f"reviewer-{i:02d}"})
                continue
            output.append({"review_id": str(uuid.uuid4()),
                           "target_verdict_id": target.get("verdict_id"), "stance": stance,
                           "challenge_type": challenge_type if stance == "CHALLENGE" else "NONE",
                           "argument": argument, "evidence_refs": [str(x) for x in refs],
                           "reviewer_alias": f"reviewer-{i:02d}", "status": "VALIDATED",
                           "created_at": _now_utc().isoformat()})
    return output


def challenge_resolution_loop(challenges: Sequence[Mapping[str, Any]],
                              resolver: Callable[[dict[str, Any], int], Mapping[str, Any]],
                              *, max_rounds: int = 3,
                              resolver_timeout_seconds: float = 30.0,
                              overall_timeout_seconds: float = 120.0) -> dict[str, Any]:
    """Resolve challenges with bounded waits; timeouts leave the challenge open."""
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds < 1 or max_rounds > 3:
        raise ValueError("max_rounds must be an integer from 1 to 3")
    if (isinstance(resolver_timeout_seconds, bool)
            or not isinstance(resolver_timeout_seconds, (int, float))
            or not math.isfinite(resolver_timeout_seconds)
            or resolver_timeout_seconds <= 0 or resolver_timeout_seconds > 120):
        raise ValueError("resolver_timeout_seconds must be finite and in (0,120]")
    if (isinstance(overall_timeout_seconds, bool)
            or not isinstance(overall_timeout_seconds, (int, float))
            or not math.isfinite(overall_timeout_seconds)
            or overall_timeout_seconds <= 0 or overall_timeout_seconds > 600):
        raise ValueError("overall_timeout_seconds must be finite and in (0,600]")
    resolution_deadline = time.monotonic() + float(overall_timeout_seconds)
    transcript: list[dict[str, Any]] = []
    resolved_challenges: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for original in challenges:
        if not isinstance(original, Mapping):
            unresolved.append({"status": "OPEN", "resolution_error": "INVALID_CHALLENGE_RECORD"})
            continue
        challenge = dict(original)
        challenge_id = str(challenge.get("challenge_id", "")).strip()
        target_verdict_id = str(challenge.get("target_verdict_id", "")).strip()
        challenge_type = str(challenge.get("challenge_type", "")).strip().upper()
        refs = challenge.get("evidence_refs")
        refs_valid = (
            isinstance(refs, Sequence) and not isinstance(refs, (str, bytes))
            and bool(refs) and all(isinstance(ref, str) and ref.strip() for ref in refs)
        )
        if not challenge_id or not target_verdict_id or not challenge_type or not refs_valid:
            challenge["status"] = "OPEN"
            challenge["resolution_error"] = "CHALLENGE_ID_TARGET_TYPE_OR_EVIDENCE_INVALID"
            unresolved.append(challenge)
            continue
        if str(challenge.get("status", "")).upper() == "RESOLVED":
            resolution_refs = challenge.get("resolution_evidence_refs")
            if (isinstance(resolution_refs, Sequence) and not isinstance(resolution_refs, (str, bytes))
                    and bool(resolution_refs) and all(isinstance(ref, str) and ref.strip() for ref in resolution_refs)):
                resolved_challenges.append(challenge)
            else:
                challenge["status"] = "OPEN"
                challenge["resolution_error"] = "PRE_RESOLVED_CHALLENGE_MISSING_RESOLUTION_EVIDENCE"
                unresolved.append(challenge)
            continue

        resolved = False
        for round_no in range(1, max_rounds + 1):
            remaining = resolution_deadline - time.monotonic()
            if remaining <= 0:
                response = {"resolved": False, "error_code": "RESOLVER_CYCLE_TIMEOUT"}
            else:
                overall_limited = remaining < float(resolver_timeout_seconds)
                raw_response, resolver_error = _run_bounded_challenge_resolver(
                    resolver, challenge, round_no,
                    min(float(resolver_timeout_seconds), remaining),
                )
                if resolver_error and overall_limited and resolver_error == "RESOLVER_TIMEOUT":
                    response = {"resolved": False, "error_code": "RESOLVER_CYCLE_TIMEOUT"}
                elif resolver_error:
                    response = {"resolved": False, "error_code": resolver_error}
                else:
                    response = dict(raw_response) if isinstance(raw_response, Mapping) else {
                        "resolved": False, "error_code": "INVALID_RESOLVER_RESPONSE"
                    }
            transcript.append({"challenge_id": challenge_id, "round": round_no, "response": response})
            response_refs = response.get("evidence_refs")
            valid_response_refs = (
                isinstance(response_refs, Sequence) and not isinstance(response_refs, (str, bytes))
                and bool(response_refs) and all(isinstance(ref, str) and ref.strip() for ref in response_refs)
            )
            if response.get("resolved") is True and valid_response_refs:
                resolved_challenges.append({
                    **challenge, "status": "RESOLVED",
                    "resolution_evidence_refs": [str(ref) for ref in response_refs],
                    "resolution_round": round_no,
                    "resolution_summary": str(response.get("summary", "")).strip()[:2000],
                })
                resolved = True
                break
            challenge["last_resolution_attempt"] = response
            if response.get("error_code") == "RESOLVER_CYCLE_TIMEOUT":
                break
        if not resolved:
            challenge["status"] = "OPEN"
            unresolved.append(challenge)

    deadlock = bool(unresolved)
    return {
        "status": "ESCALATE" if deadlock else "RESOLVED",
        "resolved_challenges": resolved_challenges,
        "open_challenges": unresolved,
        "transcript": transcript,
        "max_rounds": max_rounds,
        "trade_allowed": False,
    }


def certainty_gate(payload: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    """Conjunctive certainty checks. PASS forwards evidence to ChiefAgent only."""
    if not isinstance(payload, Mapping):
        return {"status": "GATHER_MORE", "passed": False, "trade_allowed": False,
                "blockers": ["INVALID_CERTAINTY_PAYLOAD"], "checks": {}}
    reasons: list[str] = []
    checks: dict[str, bool] = {}
    thresholds = policy.get("certainty_thresholds", policy) if isinstance(policy, Mapping) else {}
    if not isinstance(thresholds, Mapping):
        thresholds = {}
        reasons.append("INVALID_CERTAINTY_POLICY")

    def policy_number(key: str, default: float, *, lower: float, upper: float) -> float:
        value = _finite_float(thresholds.get(key, default))
        if value is None or value < lower or value > upper:
            reasons.append(f"INVALID_POLICY_VALUE:{key}")
            return default
        return value

    near = policy_number("near_certainty_threshold", 0.98, lower=0.0, upper=1.0)
    consensus_threshold = policy_number("consensus_score_threshold", 0.98, lower=0.0, upper=1.0)
    brier_threshold = policy_number("brier_threshold", 0.12, lower=0.0, upper=1.0)
    source_floor = policy_number("source_credibility_threshold", 0.75, lower=0.0, upper=1.0)
    source_max_age = policy_number("source_max_age_seconds", 900, lower=0.001, upper=31_536_000)
    min_calibration_samples = int(policy_number("min_calibration_sample_size", 30, lower=1, upper=10_000_000))
    min_reviewers_raw = _finite_float(thresholds.get("min_reviewers_per_verdict", 2))
    if min_reviewers_raw is None or min_reviewers_raw < 1 or not min_reviewers_raw.is_integer():
        reasons.append("INVALID_POLICY_VALUE:min_reviewers_per_verdict")
        min_reviewers = 2
    else:
        min_reviewers = int(min_reviewers_raw)

    if payload.get("challenge_resolution_status") == "DEADLOCK":
        return {"status": "ESCALATE", "passed": False, "trade_allowed": False,
                "blockers": ["CHALLENGE_DEADLOCK"], "checks": {"deadlock_clear": False}}
    raw_verdicts = payload.get("verdicts")
    verdicts = list(raw_verdicts) if isinstance(raw_verdicts, Sequence) and not isinstance(raw_verdicts, (str, bytes)) else []
    checks["verdicts_present"] = bool(verdicts) and all(isinstance(v, Mapping) for v in verdicts)
    if not checks["verdicts_present"]:
        reasons.append("VERDICTS_MISSING_OR_INVALID")
        verdicts = [v for v in verdicts if isinstance(v, Mapping)]

    configured_mandatory = thresholds.get("mandatory_agents")
    if configured_mandatory is None:
        configured_mandatory = payload.get("mandatory_agents", [])
    if not isinstance(configured_mandatory, Sequence) or isinstance(configured_mandatory, (str, bytes)):
        configured_mandatory = []
        reasons.append("MANDATORY_AGENT_POLICY_INVALID")
    mandatory = {str(x).strip() for x in configured_mandatory if str(x).strip()}
    checks["mandatory_agent_policy_present"] = bool(mandatory)
    if not mandatory:
        reasons.append("MANDATORY_AGENT_POLICY_EMPTY")
    seen_agents = [str(v.get("agent_id", "")).strip() for v in verdicts]
    duplicates = sorted({agent for agent in seen_agents if agent and seen_agents.count(agent) > 1})
    if duplicates:
        reasons.append("DUPLICATE_AGENT_VERDICTS:" + ",".join(duplicates))
    missing = sorted(mandatory - set(seen_agents))
    checks["all_mandatory_agents_present"] = not missing
    if missing:
        reasons.append("MISSING_MANDATORY_AGENTS:" + ",".join(missing))

    verdicts_ok = bool(verdicts) and not duplicates
    reviewers_ok = bool(verdicts)
    brier_ok = bool(verdicts)
    mandatory_approve_ok = not missing
    for v in verdicts:
        agent_id = str(v.get("agent_id", "unknown"))
        state = str(v.get("verdict", "ABSTAIN")).upper()
        confidence = _finite_float(v.get("confidence"))
        reason = str(v.get("abstain_reason", "")).strip()
        is_mandatory = agent_id in mandatory
        if confidence is None or not 0 <= confidence <= 1:
            verdicts_ok = False
            reasons.append(f"{agent_id}:INVALID_CONFIDENCE")
        if state in ("VETO", "REJECT"):
            verdicts_ok = False
            reasons.append(f"{agent_id}:{state}")
        elif state == "ABSTAIN":
            if not reason:
                verdicts_ok = False
                reasons.append(f"{agent_id}:ABSTAIN_WITHOUT_FORMAL_REASON")
            if is_mandatory:
                mandatory_approve_ok = False
                reasons.append(f"{agent_id}:MANDATORY_AGENT_ABSTAINED")
        elif state == "APPROVE":
            if confidence is None or confidence < near:
                verdicts_ok = False
                reasons.append(f"{agent_id}:CONFIDENCE_BELOW_THRESHOLD")
        else:
            verdicts_ok = False
            reasons.append(f"{agent_id}:NON_APPROVE_VERDICT")

        reviewer_count = _finite_float(v.get("reviewer_count"))
        if reviewer_count is None or reviewer_count < min_reviewers or not reviewer_count.is_integer():
            reviewers_ok = False
            reasons.append(f"{agent_id}:INSUFFICIENT_OR_INVALID_PEER_REVIEWS")

        calibration = v.get("calibration_meta")
        if not isinstance(calibration, Mapping):
            calibration = {}
        brier = _finite_float(calibration.get("brier_score"))
        sample_size = _finite_float(calibration.get("sample_size"))
        calibration_status = str(calibration.get("calibration_status", "")).upper()
        if (brier is None or not 0 <= brier <= brier_threshold or sample_size is None
                or sample_size < min_calibration_samples or not sample_size.is_integer()
                or calibration_status != "CALIBRATED"):
            brier_ok = False
            reasons.append(f"{agent_id}:CALIBRATION_MISSING_UNVERIFIED_OR_OUT_OF_LIMIT")

    checks["no_reject_veto_and_confident_approvals"] = verdicts_ok
    checks["all_mandatory_agents_approve"] = mandatory_approve_ok and not missing
    checks["all_verdicts_have_required_reviewers"] = reviewers_ok
    checks["all_agent_calibration_within_limit"] = brier_ok

    score = _finite_float(payload.get("consensus_score"))
    checks["consensus_score_threshold"] = score is not None and 0 <= score <= 1 and score >= consensus_threshold
    if not checks["consensus_score_threshold"]:
        reasons.append("CONSENSUS_SCORE_MISSING_INVALID_OR_BELOW_THRESHOLD")

    if "open_challenges" not in payload or not isinstance(payload.get("open_challenges"), Sequence) or isinstance(payload.get("open_challenges"), (str, bytes)):
        checks["no_open_challenges"] = False
        reasons.append("OPEN_CHALLENGE_STATE_MISSING_OR_INVALID")
        open_challenges = []
    else:
        open_challenges = list(payload.get("open_challenges", []))
        checks["no_open_challenges"] = not open_challenges
        if open_challenges:
            reasons.append("OPEN_CHALLENGES_PRESENT")

    if payload.get("challenge_resolution_status") not in ("RESOLVED", "NONE"):
        checks["challenge_resolution_complete"] = False
        reasons.append("CHALLENGE_RESOLUTION_STATUS_MISSING_OR_INCOMPLETE")
    else:
        checks["challenge_resolution_complete"] = True

    if "contradictions_last_n_hours" not in payload:
        checks["no_recent_contradictions"] = False
        reasons.append("CONTRADICTION_SCAN_RESULT_MISSING")
    else:
        contradiction_count = _finite_float(payload.get("contradictions_last_n_hours"))
        clear = contradiction_count is not None and contradiction_count == 0 and contradiction_count.is_integer()
        checks["no_recent_contradictions"] = clear
        if not clear:
            reasons.append("RECENT_KNOWLEDGE_GRAPH_CONTRADICTIONS_OR_INVALID_COUNT")

    raw_sources = payload.get("key_sources", [])
    sources = list(raw_sources) if isinstance(raw_sources, Sequence) and not isinstance(raw_sources, (str, bytes)) else []
    sources_ok = bool(sources)
    seen_source_ids: set[str] = set()
    independence_groups: set[str] = set()
    for source in sources:
        if not isinstance(source, Mapping):
            sources_ok = False
            continue
        source_id = str(source.get("source_id", "")).strip()
        credibility = _finite_float(source.get("credibility"))
        age = _finite_float(source.get("age_seconds"))
        ttl = _finite_float(source.get("ttl_seconds"))
        tag = str(source.get("legal_tag", "")).upper()
        if (not source_id or credibility is None or age is None or ttl is None or ttl <= 0 or age < 0
                or credibility < source_floor or age > min(source_max_age, ttl) or tag == "MNPI-SUSPECT"):
            sources_ok = False
        if source_id:
            seen_source_ids.add(source_id)
            group = str(source.get("independence_group", source_id)).strip()
            if group:
                independence_groups.add(group)
    checks["key_sources_fresh_and_credible"] = sources_ok
    if not sources_ok:
        reasons.append("KEY_SOURCE_MISSING_STALE_LOW_CREDIBILITY_OR_MNPI_SUSPECT")
    if payload.get("news_or_rumor") is True:
        min_independent_raw = _finite_float(thresholds.get("required_independent_sources_for_news_or_rumor", 2))
        min_independent = int(min_independent_raw) if min_independent_raw is not None and min_independent_raw >= 1 else 2
        independent_ok = len(independence_groups) >= min_independent and len(seen_source_ids) >= min_independent
        checks["independent_sources_sufficient"] = independent_ok
        if not independent_ok:
            reasons.append("INSUFFICIENT_INDEPENDENT_SOURCES_FOR_NEWS_OR_RUMOR")

    da = payload.get("devil_advocate_report")
    hypotheses = da.get("counter_hypotheses", []) if isinstance(da, Mapping) else []
    valid_hypotheses = [h for h in hypotheses if isinstance(h, Mapping) and str(h.get("hypothesis", "")).strip()
                        and isinstance(h.get("evidence_refs"), Sequence) and not isinstance(h.get("evidence_refs"), (str, bytes)) and len(h.get("evidence_refs")) > 0]
    da_ok = isinstance(da, Mapping) and da.get("status") == "PASS" and len(valid_hypotheses) >= 3
    checks["devil_advocate_has_three_evidence_backed_hypotheses"] = da_ok
    if not da_ok:
        reasons.append("DEVIL_ADVOCATE_REPORT_MISSING_OR_INSUFFICIENT")

    if "risk_flags" not in payload or not isinstance(payload.get("risk_flags"), Sequence) or isinstance(payload.get("risk_flags"), (str, bytes)):
        checks["no_active_risk_flags"] = False
        reasons.append("RISK_FLAGS_STATE_MISSING_OR_INVALID")
    else:
        risk_flags = [str(f).strip() for f in payload.get("risk_flags", []) if str(f).strip()]
        checks["no_active_risk_flags"] = not risk_flags
        if risk_flags:
            reasons.append("ACTIVE_RISK_FLAGS:" + ",".join(risk_flags))

    # System-health guardrails: confidence thresholds cannot stay permissive when
    # historical false-certainty rises or adversarial disagreement becomes rare.
    dissent_floor = policy_number("dissent_rate_floor", 0.10, lower=0.0, upper=1.0)
    false_certainty_limit = policy_number("false_certainty_rate_max", 0.05, lower=0.0, upper=1.0)
    min_dissent_samples = int(policy_number("min_dissent_samples", 30, lower=1, upper=10_000_000))
    min_false_certainty_samples = int(policy_number("min_false_certainty_samples", 30, lower=1, upper=10_000_000))
    dissent_rate = _finite_float(payload.get("dissent_rate"))
    dissent_sample_size = _finite_float(payload.get("dissent_sample_size"))
    dissent_ok = (dissent_rate is not None and 0 <= dissent_rate <= 1 and dissent_rate >= dissent_floor
                  and dissent_sample_size is not None and dissent_sample_size >= min_dissent_samples
                  and dissent_sample_size.is_integer())
    checks["groupthink_guard"] = dissent_ok
    if not dissent_ok:
        reasons.append("GROUPTHINK_ALARM_OR_DISSENT_METRIC_MISSING")
    false_certainty_rate = _finite_float(payload.get("false_certainty_rate"))
    false_certainty_samples = _finite_float(payload.get("false_certainty_sample_size"))
    false_certainty_ok = (false_certainty_rate is not None and 0 <= false_certainty_rate <= 1
                          and false_certainty_rate <= false_certainty_limit
                          and false_certainty_samples is not None
                          and false_certainty_samples >= min_false_certainty_samples
                          and false_certainty_samples.is_integer())
    checks["false_certainty_guard"] = false_certainty_ok
    if not false_certainty_ok:
        reasons.append("FALSE_CERTAINTY_RATE_HIGH_OR_METRIC_UNVERIFIED")

    checks["no_veto_or_reject"] = verdicts_ok
    checks["reviewer_and_calibration_requirements"] = reviewers_ok and brier_ok and mandatory_approve_ok
    passed = all(checks.values()) and not reasons
    blockers = sorted(set(reasons))
    return {"status": "PASS" if passed else "GATHER_MORE", "passed": passed,
            "trade_allowed": False, "blockers": blockers,
            "checks": checks, "consensus_score": score,
            "threshold": consensus_threshold,
            "message": "PASS only forwards a report to ChiefAgent; it never authorizes execution."}


def devil_advocate_pass(signal: Mapping[str, Any], counter_hypotheses: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not isinstance(signal, Mapping):
        signal = {}
    hypotheses = counter_hypotheses if isinstance(counter_hypotheses, Sequence) and not isinstance(counter_hypotheses, (str, bytes)) else []
    valid = [h for h in hypotheses if isinstance(h, Mapping) and str(h.get("hypothesis", "")).strip()
             and isinstance(h.get("evidence_refs"), Sequence) and not isinstance(h.get("evidence_refs"), (str, bytes))
             and bool(h.get("evidence_refs")) and all(str(x).strip() for x in h.get("evidence_refs", []))]
    passed = len(valid) >= 3
    return {"signal_id": signal.get("signal_id"), "status": "PASS" if passed else "BLOCKED",
            "counter_hypotheses": valid, "blockers": [] if passed else ["THREE_EVIDENCE_BACKED_COUNTER_HYPOTHESES_REQUIRED"],
            "trade_allowed": False}


def chief_skeptic_pass(reasoning: Mapping[str, Any], *, max_tail_risk_probability: float = 0.05,
                       now: datetime | None = None) -> dict[str, Any]:
    """Require evidence-backed completion of all nine ChiefAgent skepticism checks."""
    if not isinstance(reasoning, Mapping):
        return {"verdict": "HOLD", "passed": False, "blockers": ["INVALID_CHIEF_REASONING"], "trade_allowed": False}
    required = list("abcdefghi")
    if not str(reasoning.get("reasoning_id", "")).strip():
        blockers_identity = ["CHIEF_REASONING_ID_MISSING"]
    else:
        blockers_identity = []
    if not str(reasoning.get("signal_id", "")).strip():
        blockers_identity.append("CHIEF_SIGNAL_ID_MISSING")
    signal_input_hash = reasoning.get("signal_input_hash")
    if not isinstance(signal_input_hash, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", signal_input_hash) is None:
        blockers_identity.append("CHIEF_SIGNAL_INPUT_HASH_MISSING_OR_INVALID")
    created_at = reasoning.get("created_at")
    if not created_at:
        blockers_identity.append("CHIEF_CREATED_AT_MISSING")
    else:
        try:
            chief_created_at = _parse_time(created_at)
            current_for_chief = _current_utc(now)
            if (chief_created_at - current_for_chief).total_seconds() > 30:
                blockers_identity.append("CHIEF_CREATED_AT_IN_FUTURE")
        except (ValueError, TypeError, OverflowError):
            blockers_identity.append("CHIEF_CREATED_AT_INVALID")
    raw_scenarios = reasoning.get("tail_risk_scenarios")
    valid_scenarios = []
    if isinstance(raw_scenarios, Sequence) and not isinstance(raw_scenarios, (str, bytes)):
        for scenario in raw_scenarios:
            if not isinstance(scenario, Mapping):
                continue
            probability = _finite_float(scenario.get("probability"))
            evidence = scenario.get("evidence_refs")
            evidence_ok = (isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes))
                           and bool(evidence) and all(str(item).strip() for item in evidence))
            if (str(scenario.get("scenario", "")).strip() and probability is not None
                    and 0 <= probability <= 1 and str(scenario.get("impact", "")).strip() and evidence_ok):
                valid_scenarios.append(scenario)
    if not valid_scenarios:
        blockers_identity.append("CHIEF_TAIL_RISK_SCENARIOS_MISSING_OR_INVALID")
    required_step_names = {
        "a": "ADVERSARIAL_PASS",
        "b": "CONTRADICTION_CHECK",
        "c": "SOURCE_AUDIT",
        "d": "TAIL_RISK_CHECK",
        "e": "RED_TEAM_SELF_REVIEW",
        "f": "FULL_CHALLENGE_LEDGER_REVIEW",
        "g": "GROUPTHINK_CHECK",
        "h": "SIMILAR_REGIME_CALIBRATION",
        "i": "ERROR_PROBABILITY_SCENARIO",
    }
    steps = reasoning.get("steps", {})
    if not isinstance(steps, Mapping):
        steps = {}
    missing = [step for step in required if not isinstance(steps.get(step), Mapping)]
    failed = []
    for step in required:
        item = steps.get(step)
        if not isinstance(item, Mapping):
            continue
        evidence = item.get("evidence_refs")
        has_evidence = isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes)) and bool(evidence) and all(str(x).strip() for x in evidence)
        if item.get("check_name") != required_step_names[step]:
            failed.append(step + ":CHECK_NAME_MISMATCH")
        if item.get("passed") is not True or not str(item.get("detail", "")).strip() or not has_evidence:
            failed.append(step)
    blockers: list[str] = list(blockers_identity)
    raw_challenges = reasoning.get("open_challenges")
    if "open_challenges" not in reasoning or not isinstance(raw_challenges, Sequence) or isinstance(raw_challenges, (str, bytes)):
        open_challenges = ["INVALID_CHALLENGE_STATE"]
    else:
        open_challenges = list(raw_challenges)
    raw_tail_risk = _finite_float(reasoning.get("tail_risk_probability"))
    tail_risk_valid = raw_tail_risk is not None and 0 <= raw_tail_risk <= 1
    max_tail_risk = _finite_float(max_tail_risk_probability)
    if max_tail_risk is None or not 0 <= max_tail_risk <= 1:
        blockers.append("INVALID_CHIEF_TAIL_RISK_POLICY")
        max_tail_risk = 0.05
    reasons_against = list(dict.fromkeys(str(x).strip() for x in reasoning.get("reasons_against", []) if str(x).strip())) if isinstance(reasoning.get("reasons_against", []), Sequence) and not isinstance(reasoning.get("reasons_against", []), (str, bytes)) else []
    if len(reasons_against) < 5:
        blockers.append("CHIEF_REQUIRES_FIVE_DISTINCT_REASONS_AGAINST")
    if missing:
        blockers.append("MISSING_CHIEF_STEPS:" + ",".join(missing))
    if failed:
        blockers.append("CHIEF_STEPS_FAILED_OR_EVIDENCE_MISSING:" + ",".join(failed))
    if open_challenges:
        blockers.append("OPEN_CHALLENGES_REMAIN")
    ledger_snapshot = reasoning.get("challenge_ledger_snapshot")
    reviewed_ids = reasoning.get("reviewed_challenge_ids")
    if reasoning.get("all_challenges_read") is not True or not isinstance(ledger_snapshot, Sequence) or isinstance(ledger_snapshot, (str, bytes)) or not isinstance(reviewed_ids, Sequence) or isinstance(reviewed_ids, (str, bytes)):
        blockers.append("CHIEF_CHALLENGE_LEDGER_REVIEW_MISSING")
    else:
        snapshot_records = [x for x in ledger_snapshot if isinstance(x, Mapping)]
        if len(snapshot_records) != len(ledger_snapshot):
            blockers.append("CHIEF_CHALLENGE_LEDGER_INVALID")
        challenge_ids = sorted(str(x.get("challenge_id", "")).strip() for x in snapshot_records)
        supplied_reviewed = sorted(str(x).strip() for x in reviewed_ids)
        if not all(challenge_ids) or challenge_ids != supplied_reviewed or len(set(challenge_ids)) != len(challenge_ids):
            blockers.append("CHIEF_DID_NOT_REVIEW_ALL_CHALLENGES")
        if any(str(x.get("status", "OPEN")).upper() not in {"RESOLVED"} for x in snapshot_records):
            blockers.append("CHIEF_CHALLENGE_LEDGER_CONTAINS_UNRESOLVED_CHALLENGE")
        if reasoning.get("challenge_ledger_hash") != canonical_hash(list(ledger_snapshot)):
            blockers.append("CHIEF_CHALLENGE_LEDGER_HASH_MISMATCH")
    if not tail_risk_valid or raw_tail_risk > max_tail_risk:
        blockers.append("TAIL_RISK_MISSING_INVALID_OR_TOO_HIGH")
    reasoning_hash = str(reasoning.get("reasoning_hash", "")).strip()
    unsigned_reasoning = dict(reasoning)
    unsigned_reasoning.pop("reasoning_hash", None)
    expected_reasoning_hash = canonical_hash(unsigned_reasoning)
    if not reasoning_hash.startswith("sha256:") or len(reasoning_hash) < 16:
        blockers.append("CHIEF_REASONING_HASH_MISSING_OR_INVALID")
    elif reasoning_hash != expected_reasoning_hash:
        blockers.append("CHIEF_REASONING_HASH_MISMATCH")
    declared = str(reasoning.get("verdict", "HOLD")).upper()
    if declared == "REJECT":
        final = "REJECT"
    elif blockers or declared != "CONFIRM":
        final = "HOLD"
    else:
        final = "CONFIRM"
    return {"verdict": final, "passed": final == "CONFIRM", "blockers": blockers,
            "reasoning_hash": reasoning_hash or None, "trade_allowed": False,
            "note": "CONFIRM is an analysis verdict, not permission to trade."}


def _minute_of_day(value: str) -> int:
    hour, minute = value.split(":", 1)
    h, m = int(hour), int(minute)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("invalid UTC time")
    return h * 60 + m


def _inside_time_window(now: datetime, window: Mapping[str, Any]) -> bool:
    current = now.hour * 60 + now.minute
    start = _minute_of_day(str(window["start"]))
    end = _minute_of_day(str(window["end"]))
    return start <= current < end if start <= end else current >= start or current < end


def permission_gate(request: Mapping[str, Any], matrix: Mapping[str, Any], *,
                    now: datetime | None = None) -> dict[str, Any]:
    """Deny by default when policy, price, news, risk or expiry inputs are missing/invalid."""
    if not isinstance(request, Mapping):
        return {"status": "DENY", "allowed": False, "blockers": ["INVALID_PERMISSION_REQUEST"], "trade_allowed": False}
    policy = matrix.get("permission_matrix", matrix) if isinstance(matrix, Mapping) else {}
    if not isinstance(policy, Mapping):
        policy = {}
    blockers: list[str] = []
    try:
        current = _current_utc(now)
    except ValueError:
        current = _now_utc()
        blockers.append("INVALID_CURRENT_TIME")

    mode = str(request.get("mode", "")).lower()
    instrument = str(request.get("instrument", "")).upper()
    direction = str(request.get("direction", "")).upper()
    if not mode:
        blockers.append("MODE_REQUIRED")
    if instrument not in {str(x).upper() for x in policy.get("instruments", [])}:
        blockers.append("INSTRUMENT_NOT_ALLOWED")
    if direction not in {str(x).upper() for x in policy.get("allowed_directions", ["BUY", "SELL"])}:
        blockers.append("DIRECTION_NOT_ALLOWED")

    size = _finite_float(request.get("size"))
    max_size = _finite_float(policy.get("max_size_per_trade"))
    if size is None or max_size is None or size <= 0 or max_size <= 0 or size > max_size:
        blockers.append("SIZE_INVALID_OR_OUT_OF_POLICY")
    if mode == "live" and policy.get("live_enabled") is not True:
        blockers.append("LIVE_MODE_DISABLED")
    if mode == "demo" and policy.get("demo_enabled") is not True:
        blockers.append("DEMO_MODE_DISABLED")
    if mode not in ("demo", "live"):
        blockers.append("INVALID_MODE")
    if not str(request.get("permission_id", "")).strip(): blockers.append("PERMISSION_ID_REQUIRED")
    if request.get("chief_verdict") != "CONFIRM": blockers.append("CHIEF_NOT_CONFIRMED")
    if request.get("certainty_status") != "PASS": blockers.append("CERTAINTY_GATE_NOT_PASSED")
    if request.get("compliance_status") != "PASS": blockers.append("COMPLIANCE_NOT_PASSED")

    raw_flags = request.get("risk_flags", [])
    if not isinstance(raw_flags, Sequence) or isinstance(raw_flags, (str, bytes)):
        blockers.append("RISK_FLAGS_INVALID")
        raw_flags = ["INVALID_RISK_FLAGS"]
    flags = {str(x).strip().lower() for x in raw_flags if str(x).strip()}
    blocked = {str(x).strip().lower() for x in policy.get("blocked_flags", [])}
    if flags & blocked: blockers.append("BLOCKED_RISK_FLAGS:" + ",".join(sorted(flags & blocked)))
    if "mnpi-suspect" in flags or "mnpi" in flags:
        blockers.append("MNPI_VETO")
    if "kill_switch" in flags or "kill-switch" in flags:
        blockers.append("KILL_SWITCH_ACTIVE")

    # Session and rollover controls use UTC by design. Invalid policy blocks.
    allowed_window = policy.get("allowed_time_utc")
    if not isinstance(allowed_window, Mapping):
        blockers.append("ALLOWED_TIME_POLICY_MISSING")
    else:
        try:
            if not _inside_time_window(current, allowed_window):
                blockers.append("OUTSIDE_ALLOWED_TIME_WINDOW")
        except (KeyError, ValueError, TypeError):
            blockers.append("INVALID_ALLOWED_TIME_POLICY")
    rollover = policy.get("rollover_block_utc")
    if rollover is not None:
        if not isinstance(rollover, Mapping):
            blockers.append("INVALID_ROLLOVER_POLICY")
        else:
            try:
                if _inside_time_window(current, rollover):
                    blockers.append("ROLLOVER_TIME_BLOCK")
            except (KeyError, ValueError, TypeError):
                blockers.append("INVALID_ROLLOVER_POLICY")
    if policy.get("blocked_around_news", True) is True:
        if request.get("within_high_impact_news_window") is not False:
            blockers.append("NEWS_WINDOW_ACTIVE_OR_UNKNOWN")

    risk = request.get("risk_snapshot")
    if not isinstance(risk, Mapping):
        blockers.append("RISK_SNAPSHOT_MISSING")
        risk = {}
    required_risk_fields = ("risk_fraction", "daily_loss_fraction", "drawdown_fraction",
                            "open_positions", "correlated_open_positions", "spread_points")
    missing_risk = [key for key in required_risk_fields if risk.get(key) is None]
    if missing_risk:
        blockers.append("RISK_SNAPSHOT_FIELDS_MISSING:" + ",".join(missing_risk))
    else:
        risk_limits = {
            "risk_fraction": _finite_float(policy.get("max_risk_per_trade_fraction", 0.005)),
            "daily_loss_fraction": _finite_float(policy.get("daily_loss_limit_fraction", 0.02)),
            "drawdown_fraction": _finite_float(policy.get("max_drawdown_fraction", 0.10)),
        }
        risk_values = {key: _finite_float(risk.get(key)) for key in risk_limits}
        for key, value in risk_values.items():
            if value is None or value < 0 or value > 1 or risk_limits[key] is None or risk_limits[key] < 0 or risk_limits[key] > 1:
                blockers.append(f"INVALID_RISK_VALUE:{key}")
        if risk_values["risk_fraction"] is not None and risk_limits["risk_fraction"] is not None and risk_values["risk_fraction"] > risk_limits["risk_fraction"]:
            blockers.append("RISK_PER_TRADE_LIMIT_EXCEEDED")
        if risk_values["daily_loss_fraction"] is not None and risk_limits["daily_loss_fraction"] is not None and risk_values["daily_loss_fraction"] >= risk_limits["daily_loss_fraction"]:
            blockers.append("DAILY_LOSS_LIMIT_REACHED")
        if risk_values["drawdown_fraction"] is not None and risk_limits["drawdown_fraction"] is not None and risk_values["drawdown_fraction"] >= risk_limits["drawdown_fraction"]:
            blockers.append("MAX_DRAWDOWN_REACHED")
        spread_points = _finite_float(risk.get("spread_points"))
        max_spread = _finite_float(policy.get("max_spread_points"))
        if spread_points is None or spread_points < 0 or max_spread is None or max_spread < 0:
            blockers.append("INVALID_SPREAD_VALUE_OR_LIMIT")
        elif spread_points > max_spread:
            blockers.append("SPREAD_LIMIT_EXCEEDED")
        for key, limit_key, message in (("open_positions", "max_positions", "MAX_OPEN_POSITIONS_REACHED"),
                                        ("correlated_open_positions", "max_correlated_positions", "CORRELATED_EXPOSURE_LIMIT_REACHED")):
            count = _finite_float(risk.get(key))
            limit = _finite_float(policy.get(limit_key))
            if count is None or count < 0 or not count.is_integer() or limit is None or limit < 1 or not limit.is_integer():
                blockers.append(f"INVALID_RISK_COUNT_OR_LIMIT:{key}")
            elif count >= limit:
                blockers.append(message)

    rr = _finite_float(request.get("expected_rr"))
    min_rr = _finite_float(policy.get("min_expected_rr", 1.5))
    if rr is None or min_rr is None or rr <= 0 or min_rr < 0 or rr < min_rr:
        blockers.append("EXPECTED_RR_INVALID_OR_TOO_LOW")

    sl = request.get("sl_tp", {})
    if not isinstance(sl, Mapping):
        sl = {}
        blockers.append("SL_TP_INVALID")
    stop = _finite_float(sl.get("sl"))
    take = _finite_float(sl.get("tp"))
    if policy.get("require_stop_loss", True) and stop is None: blockers.append("STOP_LOSS_REQUIRED_OR_INVALID")
    if policy.get("require_take_profit", True) and take is None: blockers.append("TAKE_PROFIT_REQUIRED_OR_INVALID")
    if stop is not None and take is not None:
        entry = _finite_float(request.get("expected_price"))
        if entry is None or entry <= 0:
            blockers.append("EXPECTED_PRICE_MISSING_OR_INVALID")
        else:
            if direction == "BUY":
                geometry_valid = stop > 0 and stop < entry < take
                if not geometry_valid: blockers.append("INVALID_BUY_SL_TP_GEOMETRY")
            elif direction == "SELL":
                geometry_valid = take > 0 and take < entry < stop
                if not geometry_valid: blockers.append("INVALID_SELL_SL_TP_GEOMETRY")
            else:
                geometry_valid = False
            if geometry_valid:
                risk_distance = abs(entry - stop)
                reward_distance = abs(take - entry)
                calculated_rr = reward_distance / risk_distance if risk_distance > 0 else None
                tolerance_fraction = _finite_float(policy.get("expected_rr_tolerance_fraction", 0.10))
                if calculated_rr is None or rr is None or tolerance_fraction is None or not 0 <= tolerance_fraction <= 0.25:
                    blockers.append("EXPECTED_RR_RECALCULATION_FAILED")
                elif abs(rr - calculated_rr) > max(0.01, calculated_rr * tolerance_fraction):
                    blockers.append("EXPECTED_RR_DOES_NOT_MATCH_SL_TP")

    expiry = request.get("expires_at")
    if not expiry:
        blockers.append("EXPIRY_MISSING")
    else:
        try:
            expires = _parse_time(expiry)
            ttl = (expires - current).total_seconds()
            max_ttl = _finite_float(policy.get("max_command_ttl_seconds", 600))
            if ttl <= 0: blockers.append("REQUEST_EXPIRED")
            if max_ttl is None or max_ttl <= 0 or ttl > max_ttl: blockers.append("TTL_EXCEEDS_POLICY")
        except (ValueError, TypeError, OverflowError):
            blockers.append("INVALID_EXPIRY_TIMESTAMP")
    if blockers:
        return {"status": "DENY", "allowed": False, "blockers": sorted(set(blockers)), "trade_allowed": False}
    return {"status": "REQUIRE_HUMAN", "allowed": False, "blockers": [],
            "permission_id": request.get("permission_id"), "mode": mode,
            "expires_at": expiry, "trade_allowed": False,
            "next_step": "USER_AUTHORIZATION_REQUIRED"}


def user_authorization_gate(permission_result: Mapping[str, Any], decision: Mapping[str, Any], *,
                            now: datetime | None = None, max_ttl_seconds: int = 600,
                            max_future_skew_seconds: int = 30) -> dict[str, Any]:
    """Validate one-time authorization metadata; does not sign or dispatch an order."""
    blockers: list[str] = []
    if not isinstance(permission_result, Mapping) or not isinstance(decision, Mapping):
        return {"status": "DENY", "authorized": False, "blockers": ["INVALID_AUTHORIZATION_PAYLOAD"], "trade_allowed": False}
    if permission_result.get("status") != "REQUIRE_HUMAN": blockers.append("PERMISSION_NOT_PENDING_HUMAN_APPROVAL")
    if str(decision.get("decision", "")).upper() != "APPROVE": blockers.append("USER_DID_NOT_APPROVE")
    if not str(decision.get("user_id", "")).strip(): blockers.append("USER_ID_MISSING")
    if not str(decision.get("reason", "")).strip(): blockers.append("DECISION_REASON_MISSING")
    if not permission_result.get("permission_id") or decision.get("permission_id") != permission_result.get("permission_id"):
        blockers.append("PERMISSION_ID_MISMATCH")
    mode = str(permission_result.get("mode", "")).lower()
    if mode not in ("demo", "live"):
        blockers.append("PERMISSION_MODE_MISSING_OR_INVALID")
    try:
        current = _current_utc(now)
        decision_at = _parse_time(decision.get("timestamp", ""))
        age = (current - decision_at).total_seconds()
        ttl_raw = _finite_float(decision.get("ttl_seconds", max_ttl_seconds))
        if age < -max_future_skew_seconds: blockers.append("DECISION_TIMESTAMP_IN_FUTURE")
        if ttl_raw is None or ttl_raw <= 0: blockers.append("INVALID_DECISION_TTL")
        else:
            ttl = min(max_ttl_seconds, ttl_raw)
            if age > ttl: blockers.append("DECISION_EXPIRED")
        permission_expiry_raw = permission_result.get("expires_at")
        if not permission_expiry_raw:
            blockers.append("PERMISSION_EXPIRY_MISSING")
        else:
            permission_expires = _parse_time(permission_expiry_raw)
            if permission_expires <= current: blockers.append("PERMISSION_EXPIRED")
            if permission_expires < decision_at: blockers.append("DECISION_AFTER_PERMISSION_EXPIRY")
    except (ValueError, TypeError, OverflowError):
        blockers.append("INVALID_AUTHORIZATION_TIMESTAMP")
    if mode == "live":
        if decision.get("two_fa_verified") is not True: blockers.append("LIVE_2FA_REQUIRED")
        if decision.get("signature_verified") is not True: blockers.append("LIVE_SIGNATURE_REQUIRED")
    if blockers:
        return {"status": "DENY", "authorized": False, "blockers": sorted(set(blockers)), "trade_allowed": False}
    return {"status": "ALLOW_ONCE", "authorized": True, "blockers": [],
            "user_id": decision.get("user_id"), "timestamp": decision.get("timestamp"),
            "permission_id": decision.get("permission_id"), "trade_allowed": False,
            "note": "One-time authorization metadata only; execution must revalidate signature, expiry, current price, SL/TP and idempotency."}

