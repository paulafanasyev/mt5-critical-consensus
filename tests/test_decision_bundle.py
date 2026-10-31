from __future__ import annotations

import json

import pytest

from critical_consensus.audit import canonical_hash
from critical_consensus.decision_bundle import build_decision_bundle, write_decision_bundle


def _fixture():
    signal = {
        "signal_id": "handoff-001",
        "created_at": "2026-10-31T00:00:00+00:00",
        "market_snapshot": {"symbol": "EURUSD", "close": 1.1},
        "source_documents": [{"source_id": "src-official-001", "text": "official release"}],
        "available_evidence_refs": ["market-bars:sha256:abc", "src-official-001"],
        "purpose": "read-only research",
    }
    verdict = {
        "verdict_id": "v-001",
        "agent_id": "TechnicalAnalystAgent",
        "signal_id": signal["signal_id"],
        "input_hash": canonical_hash(signal),
        "created_at": "2026-10-31T00:00:00+00:00",
        "ttl_seconds": 300,
        "confidence": 0.6,
        "verdict": "ABSTAIN",
        "evidence_refs": ["src-official-001"],
    }
    collection = {
        "signal_id": signal["signal_id"],
        "status": "READY_FOR_CHIEF",
        "verdicts": [verdict],
        "peer_reviews": [{
            "review_id": "r-001", "target_verdict_id": "v-001",
            "reviewer_alias": "reviewer-01", "stance": "CONFIRM",
            "argument": "Evidence agrees", "evidence_refs": ["src-official-001"],
            "status": "VALIDATED",
        }],
        "challenge_records": [],
        "open_challenges": [],
        "challenge_resolution_status": "NONE",
        "blockers": [],
        "trade_allowed": False,
    }
    return signal, collection


def test_build_decision_bundle_preserves_exact_signal_and_review_evidence():
    signal, collection = _fixture()
    bundle = build_decision_bundle(signal, collection)

    assert bundle["signal_input"] == signal
    assert bundle["signal_input_hash"] == canonical_hash(signal)
    assert bundle["verdicts"] == collection["verdicts"]
    assert bundle["peer_reviews"] == collection["peer_reviews"]
    assert bundle["challenge_records"] == []
    assert bundle["key_sources"] == [{"source_id": "src-official-001"}]
    assert bundle["trade_allowed"] is False
    assert "chief_reasoning" not in bundle
    assert "permission_request" not in bundle
    assert "user_decision" not in bundle
    assert "order_command" not in bundle


def test_build_decision_bundle_rejects_verdict_bound_to_different_signal():
    signal, collection = _fixture()
    collection["verdicts"][0]["input_hash"] = "sha256:" + "0" * 64

    with pytest.raises(ValueError, match="input_hash"):
        build_decision_bundle(signal, collection)


def test_build_decision_bundle_rejects_forged_evidence_reference():
    signal, collection = _fixture()
    collection["verdicts"][0]["evidence_refs"] = ["not-allowlisted"]

    with pytest.raises(ValueError, match="allowlist"):
        build_decision_bundle(signal, collection)


def test_build_decision_bundle_rejects_signal_id_mismatch():
    signal, collection = _fixture()
    collection["signal_id"] = "another-signal"

    with pytest.raises(ValueError, match="signal_id"):
        build_decision_bundle(signal, collection)


def test_write_decision_bundle_round_trips_json_atomically(tmp_path):
    signal, collection = _fixture()
    bundle = build_decision_bundle(signal, collection)
    target = tmp_path / "nested" / "decision.json"

    written = write_decision_bundle(target, bundle)

    assert written == target
    assert json.loads(target.read_text(encoding="utf-8")) == bundle
    assert list(target.parent.glob(".decision.json.*.tmp")) == []


def test_handoff_enters_deterministic_pipeline_but_stays_fail_closed_without_missing_gates():
    from critical_consensus.pipeline import evaluate_signal_bundle

    signal, collection = _fixture()
    bundle = build_decision_bundle(signal, collection)

    result = evaluate_signal_bundle(bundle, {"certainty_thresholds": {}}, {})

    assert result["status"] == "GATHER_MORE"
    assert result["audit_chain_valid"] is True
    assert result["trade_allowed"] is False
    assert "AUTHORIZED_ONCE" != result["status"]
    assert "order_command" not in result
