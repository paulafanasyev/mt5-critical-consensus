from datetime import datetime, timezone, timedelta

import pytest

from critical_consensus.agent_runtime import AgentSpec, collect_agent_verdicts
from critical_consensus.market_data import MarketBar, build_market_snapshot


@pytest.mark.asyncio
async def test_market_snapshot_to_agent_collection_never_authorizes_trade():
    stamp = datetime(2026, 10, 1, tzinfo=timezone.utc)
    bars = []
    for i in range(25):
        close = 1.1 + i * 0.0001
        bars.append(MarketBar("EURUSD", "H1", stamp + timedelta(hours=i),
                              close - 0.0001, close + 0.0003, close - 0.0003,
                              close, tick_volume=50, spread_points=8))
    snapshot = build_market_snapshot(bars)
    signal_id = "snapshot-test-001"

    def make(agent_id, group):
        async def run(input_snapshot):
            assert input_snapshot["market_snapshot"]["symbol"] == "EURUSD"
            return {
                "agent_id": agent_id, "signal_id": signal_id, "verdict_id": f"v-{agent_id}",
                "input_hash": "sha256:" + "b" * 64, "confidence": 0.7,
                "verdict": "ABSTAIN", "ttl_seconds": 300,
                "created_at": datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc).isoformat(),
                "evidence_refs": ["market-snapshot:snapshot-test-001"],
            }
        return AgentSpec(agent_id, group, run)

    result = await collect_agent_verdicts(
        {"signal_id": signal_id, "market_snapshot": snapshot},
        [make("macro", "macro-feed"), make("technical", "technical-model"), make("risk", "risk-model")],
        now=datetime(2026, 10, 2, 0, 1, tzinfo=timezone.utc),
    )
    assert result["status"] == "READY_FOR_REVIEW"
    assert all(item["verdict"] == "ABSTAIN" for item in result["verdicts"])
    assert result["trade_allowed"] is False
