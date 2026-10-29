from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from critical_consensus.audit import canonical_hash, verify_audit_chain
from critical_consensus.calibration import weighted_consensus
from critical_consensus.pipeline import evaluate_signal_bundle
from jsonschema import Draft202012Validator, FormatChecker

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
CERTAINTY_POLICY = {"near_certainty_threshold": 0.98, "consensus_score_threshold": 0.98,
    "min_reviewers_per_verdict": 2, "brier_threshold": 0.12, "min_calibration_sample_size": 30,
    "dissent_rate_floor": 0.10, "min_dissent_samples": 30, "false_certainty_rate_max": 0.05,
    "min_false_certainty_samples": 30, "source_credibility_threshold": 0.75,
    "source_max_age_seconds": 900, "mandatory_agents": ["PlatformScoutAgent", "TechnicalAnalystAgent", "RiskManagerAgent", "ComplianceAgent"]}
PERMISSION_MATRIX = {"permission_matrix": {"instruments": ["EURUSD"], "max_size_per_trade": 0.2,
    "max_positions": 5, "max_correlated_positions": 2, "max_risk_per_trade_fraction": 0.005,
    "daily_loss_limit_fraction": 0.02, "max_drawdown_fraction": 0.10, "allowed_directions": ["BUY", "SELL"],
    "allowed_time_utc": {"start": "07:00", "end": "19:00"}, "rollover_block_utc": {"start": "20:55", "end": "21:10"},
    "blocked_around_news": True, "news_block_minutes_before": 15, "news_block_minutes_after": 30,
    "blocked_flags": ["sanctions", "mnpi", "mnpi_suspect", "technical_halt", "liquidity", "kill_switch"],
    "min_expected_rr": 1.5, "expected_rr_tolerance_fraction": 0.10, "require_stop_loss": True,
    "require_take_profit": True, "max_command_ttl_seconds": 600, "demo_enabled": True, "live_enabled": False}}


def make_bundle():
    agents = ["PlatformScoutAgent", "TechnicalAnalystAgent", "RiskManagerAgent", "ComplianceAgent"]
    verdicts = [{"verdict_id": f"v-{i}", "signal_id": "sig-100", "agent_id": agent,
        "verdict": "APPROVE", "confidence": 0.99, "reasoning": "Independent review of current evidence",
        "pros": ["supports thesis"], "cons": ["residual uncertainty"], "evidence_refs": [f"source-{i}"],
        "reviewer_count": 999, "created_at": NOW.isoformat(), "ttl_seconds": 300,
        "input_hash": "sha256:" + "a" * 64,
        "calibration_meta": {"brier_score": 0.05, "sample_size": 100, "method_version": "cal-v1", "calibration_status": "CALIBRATED"}}
        for i, agent in enumerate(agents)]
    reviews = []
    for i in range(len(verdicts)):
        for j in range(2):
            dissent = i == 0 and j == 0
            reviews.append({"review_id": f"review-{i}-{j}", "target_verdict_id": f"v-{i}",
                "reviewer_alias": f"reviewer-{j + 1}", "stance": "CHALLENGE" if dissent else "CONFIRM",
                "challenge_type": "LOGIC_GAP" if dissent else "NONE",
                "argument": "A calculation needs reconciliation" if dissent else "Independent cross-check supports evidence",
                "evidence_refs": [f"review-evidence-{i}-{j}"], "status": "VALIDATED", "created_at": NOW.isoformat()})
    resolved_challenge = {"challenge_id": "ch-resolved-1", "target_verdict_id": "v-0", "challenge_type": "LOGIC_GAP",
        "status": "RESOLVED", "argument": "Reconcile an independent calculation", "evidence_refs": ["review-evidence-0-0"],
        "rounds": [{"round": 1, "response": "Calculation independently reproduced", "evidence_refs": ["resolved-calculation-1"], "status": "RESOLVED"}],
        "created_at": (NOW - timedelta(seconds=10)).isoformat(), "resolved_at": NOW.isoformat(),
        "resolution_evidence_refs": ["resolved-calculation-1"]}
    challenge_snapshot = [resolved_challenge]
    bundle = {"signal_id": "sig-100", "verdicts": verdicts, "peer_reviews": reviews,
        "open_challenges": [], "challenge_records": challenge_snapshot, "challenge_resolution_status": "RESOLVED",
        "contradictions_last_n_hours": 0,
        "key_sources": [{"source_id": "s1", "credibility": 0.95, "age_seconds": 20, "ttl_seconds": 900},
                        {"source_id": "s2", "credibility": 0.94, "age_seconds": 40, "ttl_seconds": 900}],
        "devil_advocate_report": {"status": "PASS", "counter_hypotheses": [
            {"hypothesis": "adverse move", "evidence_refs": ["e1"]}, {"hypothesis": "news shock", "evidence_refs": ["e2"]},
            {"hypothesis": "regime change", "evidence_refs": ["e3"]}]},
        "risk_flags": [], "compliance_status": "PASS", "dissent_rate": 0.125, "dissent_sample_size": 80,
        "false_certainty_rate": 0.02, "false_certainty_sample_size": 80,
        "chief_reasoning": {"reasoning_id": "chief-r-100", "signal_id": "sig-100", "created_at": NOW.isoformat(),
            "verdict": "CONFIRM", "steps": {step: {"passed": True, "detail": f"checked {step}", "evidence_refs": [f"chief-{step}"]} for step in "abcdefghi"},
            "reasons_against": ["macro risk", "news risk", "correlation risk", "liquidity risk", "regime mismatch"],
            "tail_risk_probability": 0.01, "tail_risk_scenarios": [{"scenario": "central-bank surprise", "probability": 0.01,
                "impact": "rapid adverse repricing", "evidence_refs": ["stress-model-1"], "mitigation": "remain in demo and keep hard stop"}],
            "open_challenges": [], "all_challenges_read": True, "challenge_ledger_snapshot": challenge_snapshot,
            "challenge_ledger_hash": canonical_hash(challenge_snapshot), "reviewed_challenge_ids": ["ch-resolved-1"]},
        "permission_request": {"permission_id": "perm-1", "signal_id": "sig-100", "instrument": "EURUSD", "direction": "BUY",
            "size": 0.1, "mode": "demo", "expected_rr": 2.5, "expected_price": 1.0840, "sl_tp": {"sl": 1.0820, "tp": 1.0890},
            "consensus_score": 0.0, "chief_reasoning_hash": "", "tail_risk_summary": "Stress scenarios reviewed",
            "sources": [{"id": "s1", "credibility": 0.95, "age_seconds": 20}, {"id": "s2", "credibility": 0.94, "age_seconds": 40}],
            "expires_at": (NOW + timedelta(minutes=5)).isoformat(), "within_high_impact_news_window": False,
            "risk_snapshot": {"risk_fraction": 0.003, "daily_loss_fraction": 0.004, "drawdown_fraction": 0.02,
                              "open_positions": 1, "correlated_open_positions": 0}}}
    bundle["chief_reasoning"]["reasoning_hash"] = canonical_hash(bundle["chief_reasoning"])
    bundle["permission_request"]["chief_reasoning_hash"] = bundle["chief_reasoning"]["reasoning_hash"]
    bundle["permission_request"]["consensus_score"] = weighted_consensus(verdicts)
    return bundle


def test_pipeline_reaches_human_gate_but_does_not_create_order():
    bundle = make_bundle(); root = Path(__file__).resolve().parents[1]
    for name, value in (("chief_reasoning.schema.json", bundle["chief_reasoning"]),
        ("agent_verdict_v2.schema.json", bundle["verdicts"][0]), ("peer_review.schema.json", bundle["peer_reviews"][0]),
        ("challenge_record.schema.json", bundle["challenge_records"][0])):
        schema = json.loads((root / "schemas" / name).read_text(encoding="utf-8"))
        assert not list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value))
    ps = json.loads((root / "schemas" / "permission_request.schema.json").read_text(encoding="utf-8"))
    contract = dict(bundle["permission_request"], chief_verdict="CONFIRM", certainty_status="PASS", compliance_status="PASS", risk_flags=[])
    assert not list(Draft202012Validator(ps).iter_errors(contract))
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "REQUIRE_HUMAN" and result["trade_allowed"] is False
    assert result["details"]["certainty"]["status"] == "PASS" and result["details"]["chief"]["verdict"] == "CONFIRM"
    assert "order_command" not in result
    packet = result["details"]["human_review_packet"]
    assert len(packet["peer_reviews"]) == 8 and len(packet["dissenting_opinions"]) == 1
    assert packet["tail_risk_probability"] == 0.01 and result["audit_chain_valid"] is True
    assert verify_audit_chain(result["audit_events"])["valid"] is True


def test_pipeline_does_not_trust_caller_supplied_reviewer_count():
    bundle = make_bundle(); bundle["peer_reviews"] = bundle["peer_reviews"][:1]
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE" and any("INSUFFICIENT_OR_INVALID_PEER_REVIEWS" in x for x in result["blockers"])


def test_pipeline_open_challenge_blocks_certainty_gate():
    bundle = make_bundle(); bundle["peer_reviews"][0].update({"stance": "CHALLENGE", "challenge_type": "LOGIC_GAP",
        "argument": "The calculation is inconsistent", "evidence_refs": ["calc-contradiction"]})
    bundle["challenge_records"] = [{"challenge_id": "ch-1", "target_verdict_id": "v-0", "challenge_type": "LOGIC_GAP",
        "status": "OPEN", "argument": "Check the calculation", "evidence_refs": ["calc-contradiction"],
        "rounds": [{"round": 1, "response": "Check pending", "evidence_refs": ["calc-contradiction"], "status": "OPEN"}],
        "created_at": NOW.isoformat()}]
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE" and any("OPEN_CHALLENGES_PRESENT" in x for x in result["blockers"])


def test_pipeline_rejects_tampered_chief_reasoning_hash():
    bundle = make_bundle(); bundle["chief_reasoning"]["reasoning_hash"] = "sha256:" + "0" * 64
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "HOLD" and result["blockers"] == ["CHIEF_REASONING_HASH_MISMATCH"]


def test_pipeline_live_mode_denied_by_policy():
    bundle = make_bundle(); bundle["permission_request"]["mode"] = "live"
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "DENY" and "LIVE_MODE_DISABLED" in result["blockers"]


def test_pipeline_denies_human_approval_without_replay_protection():
    bundle = make_bundle(); bundle["user_decision"] = {"decision_id": "decision-1", "decision": "APPROVE",
        "user_id": "operator-1", "reason": "Reviewed dissent and tail risk", "permission_id": "perm-1",
        "timestamp": NOW.isoformat(), "ttl_seconds": 300}
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "DENY" and result["details"]["authorization"]["authorized"] is False
    assert "DURABLE_REPLAY_PROTECTION_NOT_IMPLEMENTED" in result["blockers"]
    assert result["trade_allowed"] is False and "order_command" not in result


def test_pipeline_missing_challenge_ledger_fails_closed():
    bundle = make_bundle(); bundle.pop("challenge_records")
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE" and any("CHALLENGE_LEDGER_MISSING" in x for x in result["blockers"])
    assert result["audit_chain_valid"] is True


def test_pipeline_rejects_permission_request_chief_hash_mismatch():
    bundle = make_bundle(); bundle["permission_request"]["chief_reasoning_hash"] = "sha256:" + "0" * 64
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "DENY" and "PERMISSION_CHIEF_REASONING_HASH_MISMATCH" in result["blockers"]


def test_pipeline_rejects_permission_request_consensus_mismatch():
    bundle = make_bundle(); bundle["permission_request"]["consensus_score"] = 0.1
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "DENY" and "PERMISSION_CONSENSUS_SCORE_MISMATCH" in result["blockers"]


def test_pipeline_human_packet_contains_full_challenge_ledger_and_sources():
    result = evaluate_signal_bundle(make_bundle(), CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    packet = result["details"]["human_review_packet"]
    assert packet["challenge_ledger"] == make_bundle()["challenge_records"]
    assert {s["source_id"] for s in packet["source_reports"]} == {"s1", "s2"}
    assert len(packet["chief_reasoning"]["steps"]) == 9


def test_pipeline_rejects_chief_reasoning_for_another_signal():
    bundle = make_bundle(); bundle["chief_reasoning"]["signal_id"] = "different-signal"
    unsigned = dict(bundle["chief_reasoning"]); unsigned.pop("reasoning_hash", None)
    bundle["chief_reasoning"]["reasoning_hash"] = canonical_hash(unsigned)
    bundle["permission_request"]["chief_reasoning_hash"] = canonical_hash(unsigned)
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "HOLD" and result["blockers"] == ["CHIEF_SIGNAL_ID_MISMATCH"]


def test_pipeline_rejects_permission_source_summary_tampering():
    bundle = make_bundle(); bundle["permission_request"]["sources"][0]["credibility"] = 0.99
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "DENY" and "PERMISSION_SOURCE_SUMMARY_MISMATCH" in result["blockers"]


def test_verdict_for_another_signal_is_rejected_before_consensus():
    bundle = make_bundle(); bundle["verdicts"][0]["signal_id"] = "different-signal"
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE" and result["trade_allowed"] is False
    assert "VERDICT_0:VERDICT_SIGNAL_ID_MISMATCH" in result["blockers"] and "certainty" not in result["details"]


def test_expired_verdict_is_rejected_before_consensus():
    bundle = make_bundle(); bundle["verdicts"][0]["created_at"] = (NOW - timedelta(seconds=301)).isoformat()
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE" and "VERDICT_0:VERDICT_EXPIRED" in result["blockers"]
    assert result["trade_allowed"] is False


def test_verdict_with_future_timestamp_beyond_clock_skew_is_rejected():
    bundle = make_bundle(); bundle["verdicts"][0]["created_at"] = (NOW + timedelta(seconds=31)).isoformat()
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE" and "VERDICT_0:VERDICT_CREATED_AT_IN_FUTURE" in result["blockers"]


def test_verdict_missing_or_invalid_contract_metadata_is_rejected():
    for field, value, expected in (("created_at", None, "VERDICT_CREATED_AT_MISSING_OR_INVALID"),
        ("created_at", "2026-10-29T12:00:00", "VERDICT_CREATED_AT_MISSING_OR_INVALID"),
        ("ttl_seconds", True, "VERDICT_TTL_INVALID"), ("ttl_seconds", 0, "VERDICT_TTL_INVALID"),
        ("input_hash", "sha256:bad", "VERDICT_INPUT_HASH_INVALID")):
        bundle = make_bundle(); bundle["verdicts"][0][field] = value
        result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
        assert result["status"] == "GATHER_MORE" and f"VERDICT_0:{expected}" in result["blockers"]
        assert result["trade_allowed"] is False


def test_verdict_at_exact_ttl_boundary_is_accepted():
    bundle = make_bundle(); bundle["verdicts"][0]["created_at"] = (NOW - timedelta(seconds=300)).isoformat()
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "REQUIRE_HUMAN" and result["details"]["certainty"]["status"] == "PASS"
    assert result["trade_allowed"] is False and "order_command" not in result


class _SuccessfulAuditStore:
    def __init__(self):
        self.calls = 0
        self.events = None

    def persist_trace(self, events):
        self.calls += 1
        copied = [dict(event) for event in events]
        assert verify_audit_chain(copied)["valid"] is True
        self.events = copied
        return {
            "persisted": True,
            "verified": True,
            "already_present": False,
            "event_count": len(copied),
            "trade_allowed": False,
        }


class _FailedAuditStore:
    def __init__(self, *, raises=False):
        self.raises = raises

    def persist_trace(self, events):
        if self.raises:
            raise RuntimeError("synthetic database outage")
        return {
            "persisted": False,
            "verified": False,
            "blockers": ["AUDIT_TRACE_STORE_UNAVAILABLE"],
            "trade_allowed": False,
        }


def test_pipeline_persists_complete_trace_when_audit_store_is_injected():
    store = _SuccessfulAuditStore()
    result = evaluate_signal_bundle(
        make_bundle(), CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW, audit_store=store
    )
    assert result["status"] == "REQUIRE_HUMAN"
    assert result["trade_allowed"] is False
    assert store.calls == 1
    assert store.events == result["audit_events"]
    assert result["audit_persistence"]["persisted"] is True
    assert result["audit_persistence"]["verified"] is True
    assert result["audit_chain_valid"] is True


def test_pipeline_fails_closed_when_audit_store_returns_failure():
    result = evaluate_signal_bundle(
        make_bundle(), CERTAINTY_POLICY, PERMISSION_MATRIX,
        now=NOW, audit_store=_FailedAuditStore(),
    )
    assert result["status"] == "DENY"
    assert result["trade_allowed"] is False
    assert "AUDIT_PERSISTENCE_FAILED" in result["blockers"]
    assert result["audit_persistence"]["persisted"] is False
    assert result["audit_events"][-1]["event_type"] == "AUDIT_PERSISTENCE_FAILURE"
    assert result["audit_events"][-1]["payload"]["status"] == "DENY"
    assert verify_audit_chain(result["audit_events"])["valid"] is True


def test_pipeline_fails_closed_when_audit_store_raises():
    result = evaluate_signal_bundle(
        make_bundle(), CERTAINTY_POLICY, PERMISSION_MATRIX,
        now=NOW, audit_store=_FailedAuditStore(raises=True),
    )
    assert result["status"] == "DENY"
    assert result["trade_allowed"] is False
    assert "AUDIT_PERSISTENCE_FAILED" in result["blockers"]
    assert "AUDIT_STORE_UNAVAILABLE" in result["audit_persistence"]["blockers"]
