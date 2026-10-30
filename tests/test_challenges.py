import asyncio
from critical_consensus.gates import challenge_resolution_loop, devil_advocate_pass, anonymized_peer_review, self_adversarial_pass

def test_challenge_deadlock_escalates():
    result = challenge_resolution_loop([{"challenge_id": "c1", "target_verdict_id": "v1", "challenge_type": "LOGIC_GAP", "status": "OPEN", "evidence_refs": ["ev-0"]}],
        lambda challenge, round_no: {"resolved": False, "evidence_refs": []}, max_rounds=3)
    assert result["status"] == "ESCALATE"
    assert len(result["transcript"]) == 3

def test_challenge_resolution_requires_evidence():
    result = challenge_resolution_loop([{"challenge_id": "c1", "target_verdict_id": "v1", "challenge_type": "LOGIC_GAP", "status": "OPEN", "evidence_refs": ["ev-0"]}],
        lambda challenge, round_no: {"resolved": True, "evidence_refs": ["ev-1"]}, max_rounds=3)
    assert result["status"] == "RESOLVED"
    assert len(result["resolved_challenges"]) == 1
    assert result["resolved_challenges"][0]["resolution_evidence_refs"] == ["ev-1"]

def test_devil_advocate_needs_three_evidence_backed_hypotheses():
    report = devil_advocate_pass({"signal_id": "s1"}, [
        {"hypothesis": "h1", "evidence_refs": ["e1"]},
        {"hypothesis": "h2", "evidence_refs": ["e2"]},
        {"hypothesis": "h3", "evidence_refs": ["e3"]},
    ])
    assert report["status"] == "PASS"

def test_peer_review_callback_does_not_receive_agent_id():
    received = []
    async def reviewer(target, peers):
        received.append((target.copy(), peers.copy()))
        assert "agent_id" not in target
        assert all("agent_id" not in p for p in peers)
        return {"stance": "CONFIRM", "challenge_type": "LOGIC_GAP", "argument": "specific", "evidence_refs": ["e1"]}
    verdicts = [{"agent_id": "A", "verdict_id": "v1", "verdict": "APPROVE", "confidence": 0.99},
                {"agent_id": "B", "verdict_id": "v2", "verdict": "APPROVE", "confidence": 0.99},
                {"agent_id": "C", "verdict_id": "v3", "verdict": "APPROVE", "confidence": 0.99}]
    reviewers = [("A", reviewer), ("B", reviewer), ("C", reviewer)]
    result = asyncio.run(anonymized_peer_review(verdicts, reviewers, min_reviewers_per_verdict=2))
    assert len(result) == 6
    assert all(r["reviewer_alias"].startswith("reviewer-") for r in result)


def test_mnpi_suspect_is_unconditional_veto():
    result = self_adversarial_pass(
        {"verdict_id": "v1", "verdict": "APPROVE", "confidence": 0.99},
        counterarguments=["a", "b", "c"], contradictions=[],
        sources=[{"source_id": "s1", "credibility": 0.99, "min_credibility": 0.75,
                  "age_seconds": 1, "ttl_seconds": 100, "legal_tag": "MNPI-SUSPECT"}],
        news_or_rumor=False)
    assert result["verdict"] == "VETO"
    assert "MNPI_SUSPECT_SOURCE_BLOCKED" in result["blockers"]


def test_challenge_resolver_exception_escalates_without_crashing():
    def broken_resolver(_challenge, _round_no):
        raise RuntimeError("sensitive internals not to be copied")
    result = challenge_resolution_loop([{"challenge_id": "c-fail", "target_verdict_id": "v-fail", "challenge_type": "LOGIC_GAP", "status": "OPEN", "evidence_refs": ["ev-0"]}], broken_resolver, max_rounds=2)
    assert result["status"] == "ESCALATE"
    assert all("sensitive internals" not in str(item) for item in result["transcript"])


def test_challenge_resolver_timeout_fails_closed_without_blocking_caller():
    import time

    def slow_resolver(_challenge, _round_no):
        time.sleep(0.5)
        return {"resolved": True, "evidence_refs": ["ev-1"]}

    started = time.monotonic()
    result = challenge_resolution_loop(
        [{"challenge_id": "slow-1", "target_verdict_id": "v-1",
          "challenge_type": "LOGIC_GAP", "status": "OPEN", "evidence_refs": ["ev-0"]}],
        slow_resolver, max_rounds=1, resolver_timeout_seconds=0.05,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 0.3
    assert result["status"] == "ESCALATE"
    assert result["open_challenges"][0]["last_resolution_attempt"]["error_code"] == "RESOLVER_TIMEOUT"
    assert result["trade_allowed"] is False


def test_challenge_resolver_rejects_invalid_timeout():
    import pytest

    challenge = [{"challenge_id": "c-1", "target_verdict_id": "v-1",
                  "challenge_type": "LOGIC_GAP", "status": "OPEN", "evidence_refs": ["ev-0"]}]
    with pytest.raises(ValueError, match="resolver_timeout_seconds"):
        challenge_resolution_loop(challenge, lambda *_: {}, resolver_timeout_seconds=float("inf"))
