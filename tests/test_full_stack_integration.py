"""DB-backed read-only end-to-end integration; all market/forecast rows are synthetic test fixtures."""
from __future__ import annotations

import csv
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from critical_consensus.agent_runtime import AgentSpec, collect_agent_verdicts
from critical_consensus.calibration_eval import evaluate_csv
from critical_consensus.market_data import MarketBar, build_market_snapshot, build_next_bar_forecast_contract
from critical_consensus.persistence import PostgresRepository
from critical_consensus.risk_controls import risk_controlled_permission
from critical_consensus.audit import canonical_hash


pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not configured")


@pytest.mark.asyncio
async def test_market_history_agents_forecast_calibration_audit_and_kill_switch(tmp_path):
    repo = PostgresRepository(os.environ["TEST_DATABASE_URL"])
    repo.migrate()
    symbol = "E2E" + uuid.uuid4().hex[:6].upper()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = []
    for i in range(30):
        close = 1.0 + i * 0.0005
        bars.append(MarketBar(symbol, "H1", start + timedelta(hours=i),
                              close - 0.0002, close + 0.0006, close - 0.0006, close,
                              tick_volume=100 + i, spread_points=8))
    repo.append_market_bars(bars)
    loaded = repo.get_recent_market_bars(symbol, "H1", limit=21)
    snapshot = build_market_snapshot(loaded)
    forecast_contract = build_next_bar_forecast_contract(snapshot)
    assert snapshot["symbol"] == symbol

    signal_id = "e2e-" + uuid.uuid4().hex
    now = datetime(2026, 1, 3, tzinfo=timezone.utc)

    def agent(agent_id, group):
        async def run(signal):
            assert signal["market_snapshot"]["symbol"] == symbol
            return {
                "agent_id": agent_id, "signal_id": signal_id, "verdict_id": "v-" + agent_id,
                "input_hash": canonical_hash(signal), "confidence": 0.5,
                "next_bar_up_probability": 0.61, "forecast_contract": dict(signal["forecast_contract"]),
                "verdict": "ABSTAIN", "ttl_seconds": 300, "created_at": now.isoformat(),
                "evidence_refs": ["market-bars:" + signal_id],
            }
        run.provider_origin = f"test-{agent_id}.example"
        run.provider_identity = f"test-{agent_id}.example::fixture-model-{agent_id}"
        return AgentSpec(agent_id, group, run)

    collected = await collect_agent_verdicts(
        {"signal_id": signal_id, "market_snapshot": snapshot,
         "forecast_contract": forecast_contract,
         "available_evidence_refs": ["market-bars:" + signal_id]},
        [AgentSpec("analysis-a", "provider-a", agent("analysis-a", "provider-a").run, role="analyst"),
         AgentSpec("critic-b", "provider-b", agent("critic-b", "provider-b").run, role="critic"),
         AgentSpec("chief-c", "provider-c", agent("chief-c", "provider-c").run, role="chief_review"),
         AgentSpec("risk-review", "provider-d", agent("risk-review", "provider-d").run, role="risk_review")],
        now=now + timedelta(minutes=1),
        required_roles=("analyst", "critic", "chief_review", "risk_review"),
    )
    assert collected["status"] == "READY_FOR_REVIEW"
    assert collected["trade_allowed"] is False

    # Capture the probability of the pre-defined market event, not subjective
    # confidence in the agent's qualitative APPROVE/ABSTAIN/REJECT verdict.
    forecast_observations = [
        {
            "model_id": verdict["provider_identity"],
            "signal_id": signal_id + "::" + verdict["verdict_id"],
            "issued_at": verdict["created_at"],
            "probability": verdict["next_bar_up_probability"],
            "input_sha256": verdict["input_hash"],
            "forecast_kind": "NEXT_BAR_MARKET_EVENT",
            "target_policy_version": forecast_contract["target_policy_version"],
            "target_event": forecast_contract["target_event"],
            "symbol": forecast_contract["symbol"],
            "timeframe": forecast_contract["timeframe"],
            "reference_at": forecast_contract["reference_at"],
            "reference_close": forecast_contract["reference_close"],
        }
        for verdict in collected["verdicts"]
    ]
    captured = repo.record_forecasts(forecast_observations)
    assert captured == {"inserted": 4, "identical_replays": 0, "total": 4}
    assert repo.record_forecasts(forecast_observations) == {
        "inserted": 0, "identical_replays": 4, "total": 4
    }

    assert {row["probability"] for row in forecast_observations} == {0.61}
    assert {row["probability"] for row in forecast_observations} != {v["confidence"] for v in collected["verdicts"]}
    assert repo.export_resolved_forecasts(model_id=collected["verdicts"][0]["provider_identity"]) == []

    # Synthetic rows below test the report implementation only. They are not
    # connected to the DB-backed objective event ledger and are not evidence of calibration.
    model_id = "synthetic-test-model-" + uuid.uuid4().hex[:8]
    csv_path = tmp_path / "resolved.csv"
    fields = ["model_id", "signal_id", "issued_at", "resolved_at", "probability", "outcome"]
    issued_start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for i in range(120):
            issued = issued_start + timedelta(days=i)
            # Synthetic fixture tests mechanics, not real-market model quality.
            outcome = int((i * 17 + 3) % 11 < 5)
            probability = 0.55 if i % 3 == 0 else 0.45
            forecast_id = f"cal-{i:03d}-" + uuid.uuid4().hex[:6]
            resolved = issued + timedelta(hours=1)
            writer.writerow({
                "model_id": model_id, "signal_id": forecast_id, "issued_at": issued.isoformat(),
                "resolved_at": resolved.isoformat(), "probability": probability, "outcome": outcome,
            })
    report = evaluate_csv(csv_path)
    assert report["provenance_status"] == "NOT_VERIFIED_BY_SCRIPT"
    report["dataset"]["source"] = "synthetic-ci-fixture"
    assert report["models"][model_id]["total_resolved_forecasts"] == 120
    saved = repo.store_calibration_report(report)
    assert saved["inserted"] is True
    assert repo.store_calibration_report(report)["inserted"] is False

    persisted_snapshot = {
        "observed_at": datetime.now(timezone.utc).isoformat(), "status": "DENY",
        "account_mode": "unknown", "symbol": symbol, "trade_allowed": False,
        "blockers": ["MT5_TERMINAL_NOT_CONNECTED_IN_CI"],
    }
    repo.record_mt5_risk_snapshot(persisted_snapshot)
    blocked = risk_controlled_permission(
        {"instrument": symbol, "mode": "demo"}, {"permission_matrix": {}},
        kill_switch_state=repo.get_kill_switch_state(),
        snapshot=repo.get_latest_mt5_risk_snapshot(symbol),
    )
    assert blocked["status"] == "DENY"
    assert blocked["trade_allowed"] is False
    assert "KILL_SWITCH_ACTIVE_OR_INVALID" in blocked["blockers"]
    assert "MT5_RISK_COLLECTION_NOT_PASS" in blocked["blockers"]
