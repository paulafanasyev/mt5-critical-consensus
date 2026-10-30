from __future__ import annotations

import asyncio
import pytest

from critical_consensus.review_service import make_non_author_challenge_resolver, run_peer_review_cycle


VERDICTS = [
    {"verdict_id": "v0", "agent_id": "agent-0", "verdict": "APPROVE", "confidence": 0.8,
     "reasoning_summary": "base thesis", "evidence_refs": ["market:evidence:0"]},
    {"verdict_id": "v1", "agent_id": "agent-1", "verdict": "APPROVE", "confidence": 0.8,
     "reasoning_summary": "supporting thesis", "evidence_refs": ["market:evidence:0"]},
    {"verdict_id": "v2", "agent_id": "agent-2", "verdict": "APPROVE", "confidence": 0.8,
     "reasoning_summary": "risk review", "evidence_refs": ["market:evidence:0"]},
]
ALLOWED = ["market:evidence:0", "review:evidence:1", "resolution:evidence:1"]


def reviewers_for(stance_for_v0="CHALLENGE", evidence_ref="review:evidence:1"):
    async def reviewer(target, peers):
        assert target["verdict_id"] in {"v0", "v1", "v2"}
        assert len(peers) == 2
        stance = stance_for_v0 if target["verdict_id"] == "v0" else "CONFIRM"
        return {
            "stance": stance,
            "challenge_type": "LOGIC_GAP" if stance == "CHALLENGE" else "NONE",
            "argument": "Check the evidence linkage and counterexample",
            "evidence_refs": [evidence_ref],
        }
    return [("review-provider-a", reviewer),
            ("review-provider-b", reviewer),
            ("review-provider-c", reviewer)]


@pytest.mark.asyncio
async def test_peer_review_cycle_returns_complete_evidence_backed_resolved_ledger():
    resolver_calls = []

    def resolver(challenge, round_no):
        resolver_calls.append((challenge["challenge_id"], round_no))
        return {
            "resolved": True, "evidence_refs": ["resolution:evidence:1"],
            "summary": "Counterexample checked against the trusted source snapshot",
        }

    result = await run_peer_review_cycle(
        VERDICTS, reviewers_for(), allowed_evidence_refs=ALLOWED, resolver=resolver,
        min_reviewers_per_verdict=2, max_resolution_rounds=3,
    )
    assert result["status"] == "READY_FOR_CHIEF"
    assert result["trade_allowed"] is False
    assert len(result["peer_reviews"]) == 6
    assert result["review_counts"] == {"v0": 2, "v1": 2, "v2": 2}
    assert len(result["challenge_records"]) == 2
    assert all(challenge["status"] == "RESOLVED" for challenge in result["challenge_records"])
    assert all(challenge["resolution_evidence_refs"] == ["resolution:evidence:1"]
               for challenge in result["challenge_records"])
    assert result["open_challenges"] == []
    assert result["challenge_resolution_status"] == "RESOLVED"
    assert len(resolver_calls) == 2


@pytest.mark.asyncio
async def test_peer_review_cycle_escalates_after_three_unresolved_rounds():
    resolver_calls = []

    def resolver(challenge, round_no):
        resolver_calls.append(round_no)
        return {"resolved": False, "evidence_refs": [], "summary": "insufficient"}
    result = await run_peer_review_cycle(
        VERDICTS, reviewers_for(), allowed_evidence_refs=ALLOWED, resolver=resolver,
        min_reviewers_per_verdict=2, max_resolution_rounds=3,
    )
    assert result["status"] == "ESCALATE"
    assert result["blockers"] == ["CHALLENGE_DEADLOCK"]
    assert result["trade_allowed"] is False
    assert len(result["open_challenges"]) == 2
    assert all(item["status"] == "OPEN" for item in result["open_challenges"])
    assert result["challenge_resolution_status"] == "DEADLOCK"
    assert resolver_calls == [1, 2, 3, 1, 2, 3]


@pytest.mark.asyncio
async def test_peer_review_cycle_rejects_review_evidence_outside_trusted_registry():
    result = await run_peer_review_cycle(
        VERDICTS, reviewers_for(evidence_ref="fabricated:reference"),
        allowed_evidence_refs=ALLOWED, resolver=lambda challenge, round_no: {"resolved": False},
    )
    assert result["status"] == "GATHER_MORE"
    assert "PEER_REVIEW_EVIDENCE_NOT_IN_TRUSTED_REGISTRY" in result["blockers"]
    assert result["trade_allowed"] is False
    assert result["challenge_records"] == []


@pytest.mark.asyncio
async def test_peer_review_cycle_rejects_duplicate_and_missing_evidence_registry():
    resolver = lambda challenge, round_no: {"resolved": False}
    for registry in ([], ["e1", "e1"], ["e1", ""]):
        result = await run_peer_review_cycle(
            VERDICTS, reviewers_for(), allowed_evidence_refs=registry, resolver=resolver,
        )
        assert result["status"] == "GATHER_MORE"
        assert result["trade_allowed"] is False


@pytest.mark.asyncio
async def test_peer_review_cycle_without_challenges_is_ready_for_chief_not_trade():
    result = await run_peer_review_cycle(
        VERDICTS, reviewers_for(stance_for_v0="CONFIRM"),
        allowed_evidence_refs=ALLOWED,
        resolver=lambda challenge, round_no: {"resolved": False},
    )
    assert result["status"] == "READY_FOR_CHIEF"
    assert result["challenge_resolution_status"] == "NONE"
    assert result["challenge_records"] == []
    assert result["trade_allowed"] is False


def test_challenge_resolution_is_routed_away_from_the_verdict_author():
    calls = []
    def author_resolver(challenge, round_no):
        calls.append("agent-0")
        return {"resolved": True, "evidence_refs": ["resolution:evidence:1"]}
    def independent_resolver(challenge, round_no):
        calls.append("agent-1")
        return {"resolved": True, "evidence_refs": ["resolution:evidence:1"]}
    route = make_non_author_challenge_resolver(
        [{"verdict_id": "v0", "agent_id": "agent-0"},
         {"verdict_id": "v1", "agent_id": "agent-1"}],
        [("agent-0", author_resolver), ("agent-1", independent_resolver)],
    )
    result = route({"challenge_id": "ch-0", "target_verdict_id": "v0"}, 1)
    assert result["resolved"] is True
    assert calls == ["agent-1"]


def test_challenge_resolution_without_non_author_resolver_fails_closed():
    route = make_non_author_challenge_resolver(
        [{"verdict_id": "v0", "agent_id": "agent-0"}],
        [("agent-0", lambda challenge, round_no: {"resolved": True, "evidence_refs": ["resolution:evidence:1"]})],
    )
    result = route({"challenge_id": "ch-0", "target_verdict_id": "v0"}, 1)
    assert result["resolved"] is False
    assert result["error_code"] == "NO_NON_AUTHOR_CHALLENGE_RESOLVER"
    assert result["evidence_refs"] == []



@pytest.mark.asyncio
async def test_peer_review_timeout_fails_closed_instead_of_hanging_pipeline():
    async def slow_reviewer(target, peers):
        await asyncio.sleep(0.05)
        return {
            "stance": "CONFIRM", "challenge_type": "NONE",
            "argument": "review completed too late",
            "evidence_refs": ["review:evidence:1"],
        }

    result = await run_peer_review_cycle(
        VERDICTS,
        [("review-provider-a", slow_reviewer),
         ("review-provider-b", slow_reviewer),
         ("review-provider-c", slow_reviewer)],
        allowed_evidence_refs=ALLOWED,
        resolver=lambda challenge, round_no: {"resolved": False, "evidence_refs": []},
        min_reviewers_per_verdict=2,
        review_timeout_seconds=0.001,
    )
    assert result["status"] == "GATHER_MORE"
    assert "REVIEWER_TIMEOUT" in result["blockers"]
    assert result["trade_allowed"] is False
    assert result["challenge_records"] == []


@pytest.mark.asyncio
async def test_peer_review_rejects_invalid_timeout_policy():
    async def reviewer(target, peers):
        return {
            "stance": "CONFIRM", "challenge_type": "NONE",
            "argument": "review", "evidence_refs": ["review:evidence:1"],
        }

    with pytest.raises(ValueError, match="review_timeout_seconds"):
        await run_peer_review_cycle(
            VERDICTS,
            [("review-provider-a", reviewer),
             ("review-provider-b", reviewer),
             ("review-provider-c", reviewer)],
            allowed_evidence_refs=ALLOWED,
            resolver=lambda challenge, round_no: {"resolved": False},
            review_timeout_seconds=float("inf"),
        )


@pytest.mark.asyncio
async def test_peer_review_cycle_applies_review_timeout_to_sync_challenge_resolver():
    import time

    def slow_resolver(_challenge, _round_no):
        time.sleep(0.5)
        return {"resolved": True, "evidence_refs": ["resolution:evidence:1"]}

    started = time.monotonic()
    result = await run_peer_review_cycle(
        VERDICTS, reviewers_for(), allowed_evidence_refs=ALLOWED,
        resolver=slow_resolver, min_reviewers_per_verdict=2,
        max_resolution_rounds=1, review_timeout_seconds=0.05,
    )
    assert time.monotonic() - started < 0.3
    assert result["status"] == "ESCALATE"
    assert result["challenge_resolution_status"] == "DEADLOCK"
    assert len(result["open_challenges"]) == 2
    assert all(challenge["last_resolution_attempt"]["error_code"] == "RESOLVER_TIMEOUT"
               for challenge in result["open_challenges"])
    assert result["trade_allowed"] is False
