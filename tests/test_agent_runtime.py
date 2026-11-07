from datetime import datetime, timezone, timedelta

import pytest

from critical_consensus.agent_runtime import AgentSpec, collect_agent_verdicts
from critical_consensus.audit import canonical_hash


NOW = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)


def provider(agent_id, *, signal_id="s1", confidence=0.8, ttl=300, created=None):
    async def run(signal):
        return {
            "agent_id": agent_id, "signal_id": signal_id, "verdict_id": f"v-{agent_id}",
            "input_hash": canonical_hash(signal), "confidence": confidence,
            "verdict": "APPROVE", "ttl_seconds": ttl,
            "created_at": (created or NOW).isoformat(), "evidence_refs": ["source:1"],
        }
    run.provider_origin = f"provider-{agent_id}.test"
    run.provider_identity = f"provider-{agent_id}.test::test-model-{agent_id}"
    return run


@pytest.mark.asyncio
async def test_three_declared_independent_agents_are_ready_for_review_not_trade():
    agents = [AgentSpec("a1", "macro", provider("a1")),
              AgentSpec("a2", "technical", provider("a2")),
              AgentSpec("a3", "risk", provider("a3"))]
    result = await collect_agent_verdicts({"signal_id": "s1"}, agents, now=NOW)
    assert result["status"] == "READY_FOR_REVIEW"
    assert result["trade_allowed"] is False
    assert result["independence_evidence"] == "CONFIGURATION_CHECKED_STATISTICAL_INDEPENDENCE_NOT_VERIFIED"


@pytest.mark.asyncio
async def test_one_bad_agent_blocks_batch_and_does_not_disappear_silently():
    agents = [AgentSpec("a1", "g1", provider("a1")),
              AgentSpec("a2", "g2", provider("a2", signal_id="another-signal")),
              AgentSpec("a3", "g3", provider("a3"))]
    result = await collect_agent_verdicts({"signal_id": "s1"}, agents, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert "ONE_OR_MORE_AGENTS_FAILED" in result["blockers"]
    assert result["trade_allowed"] is False


@pytest.mark.asyncio
async def test_expired_verdict_is_rejected():
    old = NOW - timedelta(hours=1)
    agents = [AgentSpec(f"a{i}", f"g{i}", provider(f"a{i}", created=old, ttl=60)) for i in range(3)]
    result = await collect_agent_verdicts({"signal_id": "s1"}, agents, now=NOW)
    assert result["valid_agent_count"] == 0
    assert result["status"] == "GATHER_MORE"


@pytest.mark.asyncio
async def test_duplicate_agent_ids_block_registry():
    agents = [AgentSpec("a", "g1", provider("a")), AgentSpec("a", "g2", provider("a"))]
    result = await collect_agent_verdicts({"signal_id": "s1"}, agents, now=NOW)
    assert result["blockers"] == ["DUPLICATE_AGENT_ID"]

@pytest.mark.asyncio
async def test_required_roles_analyst_critic_chief_and_risk_must_all_be_present():
    agents = [
        AgentSpec("a1", "g1", provider("a1"), role="analyst"),
        AgentSpec("a2", "g2", provider("a2"), role="analyst"),
        AgentSpec("a3", "g3", provider("a3"), role="risk_review"),
    ]
    result = await collect_agent_verdicts(
        {"signal_id": "s1"}, agents, now=NOW,
        required_roles=("analyst", "critic", "chief_review", "risk_review"),
    )
    assert result["status"] == "GATHER_MORE"
    assert result["missing_roles"] == ["chief_review", "critic"]
    assert result["trade_allowed"] is False


@pytest.mark.asyncio
async def test_agent_role_is_trusted_registry_metadata_not_provider_claim():
    agents = [AgentSpec("a1", "g1", provider("a1"), role="critic")]
    result = await collect_agent_verdicts({"signal_id": "s1"}, agents, now=NOW)
    assert result["verdicts"][0]["role"] == "critic"
    assert result["verdicts"][0]["confidence_kind"] == "UNCALIBRATED_SUBJECTIVE"


@pytest.mark.asyncio
async def test_duplicate_provider_origin_is_rejected_even_when_groups_have_different_names():
    a1 = provider("a1")
    a2 = provider("a2")
    a2.provider_origin = a1.provider_origin
    result = await collect_agent_verdicts(
        {"signal_id": "s1"},
        [AgentSpec("a1", "group-one", a1), AgentSpec("a2", "group-two", a2)],
        now=NOW,
    )
    assert result["status"] == "GATHER_MORE"
    assert "PROVIDER_ORIGIN_REUSED" in result["blockers"]
    assert result["trade_allowed"] is False


@pytest.mark.asyncio
async def test_missing_provider_origin_fails_closed_before_calling_agents():
    called = False
    async def unconfigured(signal):
        nonlocal called
        called = True
        return {}
    result = await collect_agent_verdicts(
        {"signal_id": "s1"}, [AgentSpec("unconfigured", "group", unconfigured)],
        now=NOW,
    )
    assert result["status"] == "GATHER_MORE"
    assert "PROVIDER_MODEL_IDENTITY_MISSING" in result["blockers"]
    assert "PROVIDER_ORIGIN_MISSING" in result["blockers"]
    assert called is False


@pytest.mark.asyncio
async def test_agent_runtime_rejects_verdict_hash_for_a_different_signal():
    async def wrong_hash(signal):
        return {
            "agent_id": "agent-a", "signal_id": signal["signal_id"], "verdict_id": "v-a",
            "input_hash": "sha256:" + "0" * 64, "confidence": 0.8, "verdict": "APPROVE",
            "ttl_seconds": 300, "created_at": NOW.isoformat(), "evidence_refs": ["source:1"],
        }
    wrong_hash.provider_origin = "provider-a.example"
    wrong_hash.provider_identity = "provider-a.example::model-a"
    result = await collect_agent_verdicts(
        {"signal_id": "s1", "market_snapshot": {"close": 1.1}},
        [AgentSpec("agent-a", "provider-a", wrong_hash)], now=NOW,
    )
    assert result["status"] == "GATHER_MORE"
    assert result["agent_errors"][0]["code"].find("INPUT_HASH_DOES_NOT_MATCH_COLLECTED_SIGNAL") >= 0
    assert result["trade_allowed"] is False


@pytest.mark.asyncio
async def test_all_policy_mandatory_agent_ids_are_required_before_any_provider_call():
    calls = []
    async def run(signal):
        calls.append(signal["signal_id"])
        return {}
    run.provider_origin = "provider.example"
    run.provider_identity = "provider.example::model"
    result = await collect_agent_verdicts(
        {"signal_id": "s1"},
        [AgentSpec("PlatformScoutAgent", "provider", run)],
        required_agent_ids=("PlatformScoutAgent", "GlobalNewsAgent", "TechnicalAnalystAgent"),
        now=NOW,
    )
    assert result["status"] == "GATHER_MORE"
    assert result["missing_agent_ids"] == ["GlobalNewsAgent", "TechnicalAnalystAgent"]
    assert calls == []
    assert result["trade_allowed"] is False
