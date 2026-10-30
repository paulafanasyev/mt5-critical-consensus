"""Trusted peer-review and bounded challenge-ledger orchestration.

Reviewer callbacks are supplied by the internal provider registry. Public request payloads
must never be used as reviewer callbacks or as authoritative review evidence.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .gates import anonymized_peer_review, challenge_resolution_loop


ReviewCallable = Callable[[dict[str, Any], list[dict[str, Any]]], Awaitable[Mapping[str, Any]]]
ChallengeResolver = Callable[[dict[str, Any], int], Mapping[str, Any]]


def make_non_author_challenge_resolver(
    verdicts: Sequence[Mapping[str, Any]],
    resolver_registry: Sequence[tuple[str, ChallengeResolver]],
) -> ChallengeResolver:
    """Route each challenge to a registered resolver other than the challenged verdict's author."""
    if not isinstance(verdicts, Sequence) or not verdicts:
        raise ValueError("verdicts are required for non-author challenge routing")
    author_by_verdict: dict[str, str] = {}
    for verdict in verdicts:
        if not isinstance(verdict, Mapping):
            raise ValueError("verdict registry contains an invalid record")
        verdict_id = str(verdict.get("verdict_id", "")).strip()
        agent_id = str(verdict.get("agent_id", "")).strip()
        if not verdict_id or not agent_id or verdict_id in author_by_verdict:
            raise ValueError("verdict and agent identities must be complete and unique")
        author_by_verdict[verdict_id] = agent_id
    if (not isinstance(resolver_registry, Sequence) or not resolver_registry
            or any(not isinstance(item, tuple) or len(item) != 2
                   or not str(item[0]).strip() or not callable(item[1])
                   for item in resolver_registry)):
        raise ValueError("resolver registry is invalid")
    resolver_ids = [str(item[0]).strip() for item in resolver_registry]
    if len(set(resolver_ids)) != len(resolver_ids):
        raise ValueError("resolver identities must be unique")

    def route(challenge: dict[str, Any], round_no: int) -> Mapping[str, Any]:
        target_agent = author_by_verdict.get(str(challenge.get("target_verdict_id", "")).strip())
        if not target_agent:
            return {"resolved": False, "evidence_refs": [], "error_code": "CHALLENGE_TARGET_AUTHOR_UNKNOWN"}
        candidates = [(agent_id, callback) for agent_id, callback in resolver_registry
                      if str(agent_id).strip() != target_agent]
        if not candidates:
            return {"resolved": False, "evidence_refs": [], "error_code": "NO_NON_AUTHOR_CHALLENGE_RESOLVER"}
        challenge_id = str(challenge.get("challenge_id", "")).strip()
        selector = hashlib.sha256(f"{challenge_id}:{round_no}".encode("utf-8")).hexdigest()
        index = int(selector[:8], 16) % len(candidates)
        _resolver_id, callback = candidates[index]
        return callback(challenge, round_no)

    return route


async def run_peer_review_cycle(
    verdicts: Sequence[Mapping[str, Any]],
    reviewers: Sequence[tuple[str, ReviewCallable]],
    *,
    allowed_evidence_refs: Sequence[str],
    resolver: ChallengeResolver,
    min_reviewers_per_verdict: int = 2,
    max_resolution_rounds: int = 3,
    review_timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Create anonymized reviews and resolve registered challenges without author self-review."""
    if (not isinstance(allowed_evidence_refs, Sequence)
            or isinstance(allowed_evidence_refs, (str, bytes))
            or not allowed_evidence_refs
            or any(not isinstance(ref, str) or not ref.strip() for ref in allowed_evidence_refs)):
        return {
            "status": "GATHER_MORE", "blockers": ["TRUSTED_EVIDENCE_REGISTRY_MISSING_OR_INVALID"],
            "peer_reviews": [], "challenge_records": [], "open_challenges": [],
            "challenge_resolution_status": "MISSING", "trade_allowed": False,
        }
    allowed_list = [ref.strip() for ref in allowed_evidence_refs]
    if len(set(allowed_list)) != len(allowed_list):
        return {
            "status": "GATHER_MORE", "blockers": ["TRUSTED_EVIDENCE_REGISTRY_HAS_DUPLICATE_IDS"],
            "peer_reviews": [], "challenge_records": [], "open_challenges": [],
            "challenge_resolution_status": "MISSING", "trade_allowed": False,
        }
    allowed = set(allowed_list)
    if not callable(resolver) or max_resolution_rounds < 1 or max_resolution_rounds > 3:
        return {
            "status": "GATHER_MORE", "blockers": ["CHALLENGE_RESOLVER_OR_ROUND_POLICY_INVALID"],
            "peer_reviews": [], "challenge_records": [], "open_challenges": [],
            "challenge_resolution_status": "MISSING", "trade_allowed": False,
        }
    if isinstance(min_reviewers_per_verdict, bool) or min_reviewers_per_verdict < 1:
        return {
            "status": "GATHER_MORE", "blockers": ["PEER_REVIEWER_POLICY_INVALID"],
            "peer_reviews": [], "challenge_records": [], "open_challenges": [],
            "challenge_resolution_status": "MISSING", "trade_allowed": False,
        }
    if not verdicts or any(not isinstance(item, Mapping) for item in verdicts):
        return {
            "status": "GATHER_MORE", "blockers": ["VERDICT_SET_INVALID"],
            "peer_reviews": [], "challenge_records": [], "open_challenges": [],
            "challenge_resolution_status": "MISSING", "trade_allowed": False,
        }
    verdict_ids = [str(item.get("verdict_id", "")).strip() for item in verdicts]
    agent_ids = [str(item.get("agent_id", "")).strip() for item in verdicts]
    if (any(not item for item in verdict_ids + agent_ids)
            or len(set(verdict_ids)) != len(verdict_ids)
            or len(set(agent_ids)) != len(agent_ids)):
        return {
            "status": "GATHER_MORE", "blockers": ["VERDICT_OR_AGENT_IDENTITIES_MISSING_OR_DUPLICATE"],
            "peer_reviews": [], "challenge_records": [], "open_challenges": [],
            "challenge_resolution_status": "MISSING", "trade_allowed": False,
        }

    reviews = await anonymized_peer_review(
        verdicts, reviewers, min_reviewers_per_verdict=min_reviewers_per_verdict,
        review_timeout_seconds=review_timeout_seconds,
    )
    blockers: list[str] = []
    review_counts = {verdict_id: 0 for verdict_id in verdict_ids}
    for review in reviews:
        if review.get("status") != "VALIDATED":
            blockers.append(str(review.get("reason", "PEER_REVIEW_BLOCKED")))
            continue
        target_id = str(review.get("target_verdict_id", ""))
        if target_id not in review_counts:
            blockers.append("PEER_REVIEW_TARGET_NOT_FOUND")
            continue
        refs = review.get("evidence_refs")
        if (not isinstance(refs, Sequence) or isinstance(refs, (str, bytes))
                or not refs or any(str(ref).strip() not in allowed for ref in refs)):
            blockers.append("PEER_REVIEW_EVIDENCE_NOT_IN_TRUSTED_REGISTRY")
            continue
        review_counts[target_id] += 1

    insufficient = sorted(target for target, count in review_counts.items()
                          if count < min_reviewers_per_verdict)
    if insufficient:
        blockers.append("INSUFFICIENT_PEER_REVIEWS:" + ",".join(insufficient))
    if blockers:
        return {
            "status": "GATHER_MORE", "blockers": sorted(set(blockers)),
            "peer_reviews": reviews, "review_counts": review_counts,
            "challenge_records": [], "open_challenges": [],
            "challenge_resolution_status": "MISSING", "trade_allowed": False,
        }

    challenges: list[dict[str, Any]] = []
    for review in reviews:
        if str(review.get("stance", "")).upper() != "CHALLENGE":
            continue
        challenges.append({
            "challenge_id": "ch-" + uuid.uuid4().hex,
            "target_verdict_id": str(review["target_verdict_id"]),
            "challenge_type": str(review["challenge_type"]).upper(),
            "status": "OPEN",
            "evidence_refs": list(review["evidence_refs"]),
            "originating_review_id": review.get("review_id"),
            "originating_reviewer_alias": review.get("reviewer_alias"),
        })

    if not challenges:
        return {
            "status": "READY_FOR_CHIEF", "blockers": [],
            "peer_reviews": reviews, "review_counts": review_counts,
            "challenge_records": [], "open_challenges": [],
            "challenge_resolution_status": "NONE",
            "resolution_transcript": [], "trade_allowed": False,
        }

    resolution = challenge_resolution_loop(
        challenges, resolver, max_rounds=max_resolution_rounds,
        resolver_timeout_seconds=review_timeout_seconds,
    )
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for challenge in resolution.get("resolved_challenges", []):
        refs = challenge.get("resolution_evidence_refs", [])
        if (not isinstance(refs, Sequence) or isinstance(refs, (str, bytes))
                or not refs or any(str(ref).strip() not in allowed for ref in refs)):
            unresolved.append({**dict(challenge), "status": "OPEN",
                               "resolution_error": "RESOLUTION_EVIDENCE_NOT_IN_TRUSTED_REGISTRY"})
        else:
            resolved.append(dict(challenge))
    unresolved.extend(dict(item) for item in resolution.get("open_challenges", []))
    ledger = resolved + unresolved
    deadlock = bool(unresolved)
    return {
        "status": "ESCALATE" if deadlock else "READY_FOR_CHIEF",
        "blockers": ["CHALLENGE_DEADLOCK"] if deadlock else [],
        "peer_reviews": reviews,
        "review_counts": review_counts,
        "challenge_records": ledger,
        "open_challenges": unresolved,
        "challenge_resolution_status": "DEADLOCK" if deadlock else "RESOLVED",
        "resolution_transcript": resolution.get("transcript", []),
        "max_resolution_rounds": max_resolution_rounds,
        "trade_allowed": False,
    }
