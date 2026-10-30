from datetime import datetime, timezone, timedelta

import pytest

from critical_consensus.agent_runtime import AgentSpec, collect_agent_verdicts


NOW = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)


def provider(agent_id, *, signal_id="s1", confidence=0.8, ttl=300, created=None):
    async def run(signal):
        return {
            "agent_id": agent_id, "signal_id": signal_id, "verdict_id": f"v-{agent_id}",
            "input_hash": "sha256:" + "a" * 64, "confidence": confidence,
            "verdict": "APPROVE", "ttl_seconds": ttl,
            "created_at": (created or NOW).isoformat(), "evidence_refs": ["source:1"],
        }
    return run


@pytest.mark.asyncio
async def test_three_declared_independent_agents_are_ready_for_review_not_trade():
    agents = [AgentSpec("a1", "macro", provider("a1")),
              AgentSpec("a2", "technical", provider("a2")),
              AgentSpec("a3", "risk", provider("a3"))]
    result = await collect_agent_verdicts({"signal_id": "s1"}, agents, now=NOW)
    assert result["status"] == "READY_FOR_REVIEW"
    assert result["trade_allowed"] is False
    assert result["independence_evidence"] == "DECLARED_ONLY_NOT_VERIFIED"


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
