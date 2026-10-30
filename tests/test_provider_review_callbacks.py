import asyncio

import pytest

from critical_consensus.providers import OpenAICompatibleAgent


def make_agent():
    return OpenAICompatibleAgent(
        agent_id="reviewer", role="critic", model="review-model",
        api_key="test-secret", base_url="https://review.example/v1",
    )


@pytest.mark.asyncio
async def test_provider_blind_peer_review_accepts_only_allowlisted_evidence(monkeypatch):
    agent = make_agent()
    monkeypatch.setattr(agent, "_request_json_sync", lambda **kwargs: {
        "stance": "CHALLENGE", "challenge_type": "LOGIC_GAP",
        "argument": "The peer omitted an adverse-regime counterexample",
        "evidence_refs": ["ev-1"],
    })
    callback = agent.make_peer_reviewer(["ev-1", "ev-2"])
    result = await callback(
        {"verdict_id": "v-1", "verdict": "APPROVE", "evidence_refs": ["ev-1"]},
        [{"verdict_id": "v-2", "verdict": "ABSTAIN"}],
    )
    assert result["stance"] == "CHALLENGE"
    assert result["challenge_type"] == "LOGIC_GAP"
    assert result["evidence_refs"] == ["ev-1"]


@pytest.mark.asyncio
async def test_provider_peer_review_rejects_fabricated_source_id(monkeypatch):
    agent = make_agent()
    monkeypatch.setattr(agent, "_request_json_sync", lambda **kwargs: {
        "stance": "CONFIRM", "challenge_type": "NONE",
        "argument": "Looks valid", "evidence_refs": ["invented-source"],
    })
    callback = agent.make_peer_reviewer(["ev-1"])
    with pytest.raises(RuntimeError, match="peer_review_invalid"):
        await callback({"verdict_id": "v-1"}, [])


def test_provider_challenge_resolver_requires_allowlisted_resolution_evidence(monkeypatch):
    agent = make_agent()
    monkeypatch.setattr(agent, "_request_json_sync", lambda **kwargs: {
        "resolved": True, "summary": "The calculation was reconciled to the source record",
        "evidence_refs": ["ev-2"],
    })
    resolver = agent.make_challenge_resolver(["ev-1", "ev-2"])
    result = resolver({"challenge_id": "ch-1", "target_verdict_id": "v-1"}, 1)
    assert result["resolved"] is True
    assert result["evidence_refs"] == ["ev-2"]


def test_provider_challenge_resolver_rejects_fabricated_evidence(monkeypatch):
    agent = make_agent()
    monkeypatch.setattr(agent, "_request_json_sync", lambda **kwargs: {
        "resolved": True, "summary": "Trust me", "evidence_refs": ["not-in-catalog"],
    })
    resolver = agent.make_challenge_resolver(["ev-1"])
    with pytest.raises(RuntimeError, match="resolution_evidence_invalid"):
        resolver({"challenge_id": "ch-1"}, 1)


def test_provider_challenge_resolver_can_abstain_without_fake_resolution(monkeypatch):
    agent = make_agent()
    monkeypatch.setattr(agent, "_request_json_sync", lambda **kwargs: {
        "resolved": False, "summary": "Insufficient independent evidence", "evidence_refs": [],
    })
    resolver = agent.make_challenge_resolver(["ev-1"])
    result = resolver({"challenge_id": "ch-1"}, 1)
    assert result["resolved"] is False
    assert result["evidence_refs"] == []
