from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from critical_consensus.audit import canonical_hash, verify_audit_chain
from critical_consensus.calibration import weighted_consensus
from critical_consensus.pipeline import evaluate_signal_bundle
from critical_consensus.risk_controls import risk_request_binding_hash
from jsonschema import Draft202012Validator

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)

CERTAINTY_POLICY = {
    "near_certainty_threshold": 0.98,
    "consensus_score_threshold": 0.98,
    "min_reviewers_per_verdict": 2,
    "brier_threshold": 0.12,
    "min_calibration_sample_size": 30,
    "dissent_rate_floor": 0.10, "min_dissent_samples": 30,
    "false_certainty_rate_max": 0.05, "min_false_certainty_samples": 30,
    "source_credibility_threshold": 0.75,
    "source_max_age_seconds": 900,
    "mandatory_agents": ["PlatformScoutAgent", "TechnicalAnalystAgent", "RiskManagerAgent", "ComplianceAgent"],
}

PERMISSION_MATRIX = {
    "permission_matrix": {
        "instruments": ["EURUSD"], "max_size_per_trade": 0.2, "max_positions": 5,
        "max_correlated_positions": 2, "max_risk_per_trade_fraction": 0.005,
        "daily_loss_limit_fraction": 0.02, "max_drawdown_fraction": 0.10,
        "allowed_directions": ["BUY", "SELL"], "allowed_time_utc": {"start": "07:00", "end": "19:00"},
        "rollover_block_utc": {"start": "20:55", "end": "21:10"}, "blocked_around_news": True,
        "news_block_minutes_before": 15, "news_block_minutes_after": 30,
        "blocked_flags": ["sanctions", "mnpi", "mnpi_suspect", "technical_halt", "liquidity", "kill_switch"],
        "min_expected_rr": 1.5, "expected_rr_tolerance_fraction": 0.10,
        "require_stop_loss": True, "require_take_profit": True, "max_command_ttl_seconds": 600,
        "demo_enabled": True, "live_enabled": False,
    }
}


def make_bundle():
    agents = ["PlatformScoutAgent", "TechnicalAnalystAgent", "RiskManagerAgent", "ComplianceAgent"]
    signal_input = {"signal_id": "sig-100", "instrument": "EURUSD",
                    "market_snapshot_hash": "sha256:" + "c" * 64,
                    "available_evidence_refs": ["market-bars:EURUSD:H1","s1","s2","review-evidence-0-0","review-evidence-0-1","review-evidence-1-0","review-evidence-1-1","review-evidence-2-0","review-evidence-2-1","review-evidence-3-0","review-evidence-3-1","resolved-calculation-1","e1","e2","e3","stress-model-1","chief-a","chief-b","chief-c","chief-d","chief-e","chief-f","chief-g","chief-h","chief-i"]}
    signal_input_hash = canonical_hash(signal_input)
    verdicts = [
        {"verdict_id": f"v-{i}", "signal_id": "sig-100", "agent_id": agent,
         "verdict": "APPROVE", "confidence": 0.99,
         "reviewer_count": 999, "created_at": NOW.isoformat(), "ttl_seconds": 300,
         "input_hash": signal_input_hash, "evidence_refs": ["market-bars:EURUSD:H1"],
         "calibration_meta": {"brier_score": 0.05, "sample_size": 100,
                               "calibration_status": "CALIBRATED"}}
        for i, agent in enumerate(agents)
    ]
    reviews = []
    for i in range(len(verdicts)):
        for j in range(2):
            is_dissent = i == 0 and j == 0
            reviews.append({"target_verdict_id": f"v-{i}", "reviewer_alias": f"reviewer-{j + 1}",
                            "stance": "CHALLENGE" if is_dissent else "CONFIRM",
                            "challenge_type": "LOGIC_GAP" if is_dissent else "NONE",
                            "argument": "A calculation needs reconciliation" if is_dissent else "Independent cross-check supports the evidence",
                            "evidence_refs": [f"review-evidence-{i}-{j}"], "status": "VALIDATED"})
    resolved_challenge = {"challenge_id": "ch-resolved-1", "target_verdict_id": "v-0", "challenge_type": "LOGIC_GAP",
                          "status": "RESOLVED", "evidence_refs": ["review-evidence-0-0"],
                          "resolution_evidence_refs": ["resolved-calculation-1"]}
    challenge_snapshot = [resolved_challenge]
    bundle = {
        "signal_id": "sig-100", "signal_input": signal_input,
        "signal_input_hash": signal_input_hash, "verdicts": verdicts, "peer_reviews": reviews,
        "open_challenges": [], "challenge_records": challenge_snapshot, "challenge_resolution_status": "RESOLVED",
        "contradictions_last_n_hours": 0,
        "key_sources": [
            {"source_id": "s1", "credibility": 0.95, "age_seconds": 20, "ttl_seconds": 900},
            {"source_id": "s2", "credibility": 0.94, "age_seconds": 40, "ttl_seconds": 900},
        ],
        "devil_advocate_report": {"status": "PASS", "counter_hypotheses": [
            {"hypothesis": "adverse move", "evidence_refs": ["e1"]},
            {"hypothesis": "news shock", "evidence_refs": ["e2"]},
            {"hypothesis": "regime change", "evidence_refs": ["e3"]},
        ]},
        "risk_flags": [], "compliance_status": "PASS",
        "dissent_rate": 0.125, "dissent_sample_size": 80,
        "false_certainty_rate": 0.02, "false_certainty_sample_size": 80,
        "chief_reasoning": {
            "reasoning_id": "chief-r-100", "signal_id": "sig-100",
            "signal_input_hash": signal_input_hash, "created_at": NOW.isoformat(),
            "verdict": "CONFIRM",
            "steps": {step: {"passed": True, "check_name": {"a":"ADVERSARIAL_PASS","b":"CONTRADICTION_CHECK","c":"SOURCE_AUDIT","d":"TAIL_RISK_CHECK","e":"RED_TEAM_SELF_REVIEW","f":"FULL_CHALLENGE_LEDGER_REVIEW","g":"GROUPTHINK_CHECK","h":"SIMILAR_REGIME_CALIBRATION","i":"ERROR_PROBABILITY_SCENARIO"}[step], "detail": f"checked {step}", "evidence_refs": [f"chief-{step}"]}
                      for step in "abcdefghi"},
            "reasons_against": ["macro risk", "news risk", "correlation risk", "liquidity risk", "regime mismatch"],
            "tail_risk_probability": 0.01,
            "tail_risk_scenarios": [{"scenario": "central-bank surprise", "probability": 0.01,
                                     "impact": "rapid adverse repricing", "evidence_refs": ["stress-model-1"],
                                     "mitigation": "remain in demo and keep hard stop"}],
            "open_challenges": [],
            "all_challenges_read": True, "challenge_ledger_snapshot": challenge_snapshot,
            "challenge_ledger_hash": canonical_hash(challenge_snapshot), "reviewed_challenge_ids": ["ch-resolved-1"],
        },
        "permission_request": {
            "permission_id": "perm-1", "signal_id": "sig-100", "instrument": "EURUSD", "direction": "BUY", "size": 0.1, "mode": "demo",
            "expected_rr": 2.5, "expected_price": 1.0840, "sl_tp": {"sl": 1.0820, "tp": 1.0890},
            "consensus_score": 0.0, "chief_reasoning_hash": "", "tail_risk_summary": "Stress scenarios reviewed",
            "sources": [{"id": "s1", "credibility": 0.95, "age_seconds": 20}, {"id": "s2", "credibility": 0.94, "age_seconds": 40}],
            "expires_at": (NOW + timedelta(minutes=5)).isoformat(), "within_high_impact_news_window": False,
            "risk_snapshot": {"risk_fraction": 0.003, "daily_loss_fraction": 0.004, "drawdown_fraction": 0.02,
                              "open_positions": 1, "correlated_open_positions": 0},
        },
    }
    bundle["chief_reasoning"]["reasoning_hash"] = canonical_hash(bundle["chief_reasoning"])
    bundle["permission_request"]["chief_reasoning_hash"] = bundle["chief_reasoning"]["reasoning_hash"]
    bundle["permission_request"]["consensus_score"] = weighted_consensus(verdicts)
    return bundle


def test_pipeline_reaches_human_gate_but_does_not_create_order():
    bundle = make_bundle()
    root = Path(__file__).resolve().parents[1]
    chief_schema = json.loads((root / "schemas" / "chief_reasoning.schema.json").read_text(encoding="utf-8"))
    permission_schema = json.loads((root / "schemas" / "permission_request.schema.json").read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(chief_schema).iter_errors(bundle["chief_reasoning"]))
    permission_contract = dict(bundle["permission_request"], chief_verdict="CONFIRM", certainty_status="PASS",
                               compliance_status="PASS", risk_flags=[])
    assert not list(Draft202012Validator(permission_schema).iter_errors(permission_contract))
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "REQUIRE_HUMAN"
    assert result["trade_allowed"] is False
    assert result["details"]["certainty"]["status"] == "PASS"
    assert result["details"]["chief"]["verdict"] == "CONFIRM"
    assert "order_command" not in result
    packet = result["details"]["human_review_packet"]
    assert len(packet["peer_reviews"]) == 8
    assert len(packet["dissenting_opinions"]) == 1
    assert packet["tail_risk_probability"] == 0.01
    assert result["audit_chain_valid"] is True
    assert verify_audit_chain(result["audit_events"])["valid"] is True


def test_pipeline_does_not_trust_caller_supplied_reviewer_count():
    bundle = make_bundle()
    bundle["peer_reviews"] = bundle["peer_reviews"][:1]
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert any("INSUFFICIENT_OR_INVALID_PEER_REVIEWS" in x for x in result["blockers"])


def test_pipeline_open_challenge_blocks_certainty_gate():
    bundle = make_bundle()
    bundle["peer_reviews"][0] = {"target_verdict_id": "v-0", "reviewer_alias": "reviewer-1",
        "stance": "CHALLENGE", "challenge_type": "LOGIC_GAP", "argument": "The calculation is inconsistent",
        "evidence_refs": ["review-evidence-0-0"], "status": "VALIDATED"}
    bundle["challenge_records"] = [{"challenge_id": "ch-1", "target_verdict_id": "v-0", "challenge_type": "LOGIC_GAP",
                                     "status": "OPEN", "evidence_refs": ["calc-contradiction"]}]
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert any("OPEN_CHALLENGES_PRESENT" in x for x in result["blockers"])


def test_pipeline_rejects_tampered_chief_reasoning_hash():
    bundle = make_bundle()
    bundle["chief_reasoning"]["reasoning_hash"] = "sha256:forged-value-which-is-long-enough"
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "HOLD"
    assert result["blockers"] == ["CHIEF_REASONING_HASH_MISMATCH"]


def test_pipeline_live_mode_denied_by_policy():
    bundle = make_bundle()
    bundle["permission_request"]["mode"] = "live"
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "DENY"
    assert "LIVE_MODE_DISABLED" in result["blockers"]


def test_pipeline_valid_human_approval_is_only_authorized_once():
    bundle = make_bundle()
    bundle["user_decision"] = {"decision": "APPROVE", "user_id": "operator-1", "reason": "Reviewed dissent and tail risk",
                               "permission_id": "perm-1", "timestamp": NOW.isoformat(), "ttl_seconds": 300}
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "AUTHORIZED_ONCE"
    assert result["details"]["authorization"]["authorized"] is True
    assert result["trade_allowed"] is False
    assert "order_command" not in result


def test_pipeline_missing_challenge_ledger_fails_closed():
    bundle = make_bundle()
    bundle.pop("challenge_records")
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert any("CHALLENGE_LEDGER_MISSING" in x for x in result["blockers"])
    assert result["audit_chain_valid"] is True


def test_pipeline_rejects_permission_request_chief_hash_mismatch():
    bundle = make_bundle()
    bundle["permission_request"]["chief_reasoning_hash"] = "sha256:" + "0" * 64
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "DENY"
    assert "PERMISSION_CHIEF_REASONING_HASH_MISMATCH" in result["blockers"]


def test_pipeline_rejects_permission_request_consensus_mismatch():
    bundle = make_bundle()
    bundle["permission_request"]["consensus_score"] = 0.1
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "DENY"
    assert "PERMISSION_CONSENSUS_SCORE_MISMATCH" in result["blockers"]


def test_pipeline_human_packet_contains_full_challenge_ledger_and_sources():
    result = evaluate_signal_bundle(make_bundle(), CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    packet = result["details"]["human_review_packet"]
    assert packet["challenge_ledger"] == make_bundle()["challenge_records"]
    assert {s["source_id"] for s in packet["source_reports"]} == {"s1", "s2"}
    assert len(packet["chief_reasoning"]["steps"]) == 9


def test_pipeline_rejects_chief_reasoning_for_another_signal():
    bundle = make_bundle()
    bundle["chief_reasoning"]["signal_id"] = "different-signal"
    unsigned = dict(bundle["chief_reasoning"])
    unsigned.pop("reasoning_hash", None)
    bundle["chief_reasoning"]["reasoning_hash"] = canonical_hash(unsigned)
    bundle["permission_request"]["chief_reasoning_hash"] = canonical_hash(unsigned)
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "HOLD"
    assert result["blockers"] == ["CHIEF_SIGNAL_ID_MISMATCH"]


def test_pipeline_rejects_permission_source_summary_tampering():
    bundle = make_bundle()
    bundle["permission_request"]["sources"][0]["credibility"] = 0.99
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "DENY"
    assert "PERMISSION_SOURCE_SUMMARY_MISMATCH" in result["blockers"]


def test_verdict_for_another_signal_is_rejected_before_consensus():
    bundle = make_bundle()
    bundle["verdicts"][0]["signal_id"] = "different-signal"
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert result["trade_allowed"] is False
    assert "VERDICT_0:VERDICT_SIGNAL_ID_MISMATCH" in result["blockers"]
    assert "certainty" not in result["details"]


def test_expired_verdict_is_rejected_before_consensus():
    bundle = make_bundle()
    bundle["verdicts"][0]["created_at"] = (NOW - timedelta(seconds=301)).isoformat()
    bundle["verdicts"][0]["ttl_seconds"] = 300
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert "VERDICT_0:VERDICT_EXPIRED" in result["blockers"]
    assert result["trade_allowed"] is False


def test_verdict_with_future_timestamp_beyond_clock_skew_is_rejected():
    bundle = make_bundle()
    bundle["verdicts"][0]["created_at"] = (NOW + timedelta(seconds=31)).isoformat()
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert "VERDICT_0:VERDICT_CREATED_AT_IN_FUTURE" in result["blockers"]


def test_verdict_missing_or_invalid_contract_metadata_is_rejected():
    for field, value, expected_blocker in (
        ("created_at", None, "VERDICT_CREATED_AT_MISSING_OR_INVALID"),
        ("created_at", "2026-10-29T12:00:00", "VERDICT_CREATED_AT_MISSING_OR_INVALID"),
        ("ttl_seconds", True, "VERDICT_TTL_INVALID"),
        ("ttl_seconds", 0, "VERDICT_TTL_INVALID"),
        ("input_hash", "sha256:bad", "VERDICT_INPUT_HASH_INVALID"),
    ):
        bundle = make_bundle()
        bundle["verdicts"][0][field] = value
        result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
        assert result["status"] == "GATHER_MORE"
        assert f"VERDICT_0:{expected_blocker}" in result["blockers"]
        assert result["trade_allowed"] is False


def test_verdict_at_exact_ttl_boundary_is_accepted():
    bundle = make_bundle()
    bundle["verdicts"][0]["created_at"] = (NOW - timedelta(seconds=300)).isoformat()
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "REQUIRE_HUMAN"
    assert result["details"]["certainty"]["status"] == "PASS"
    assert result["trade_allowed"] is False
    assert "order_command" not in result


def test_pipeline_rejects_tampered_signal_input_hash_before_consensus():
    bundle = make_bundle()
    bundle["signal_input_hash"] = "sha256:" + "0" * 64
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert result["blockers"] == ["SIGNAL_INPUT_HASH_MISMATCH"]
    assert "certainty" not in result["details"]
    assert result["audit_chain_valid"] is True


def test_pipeline_binds_every_agent_verdict_to_the_same_input_snapshot():
    bundle = make_bundle()
    bundle["verdicts"][0]["input_hash"] = "sha256:" + "f" * 64
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert "VERDICT_0:VERDICT_INPUT_HASH_DOES_NOT_MATCH_SIGNAL_INPUT" in result["blockers"]
    assert "certainty" not in result["details"]


def test_pipeline_rejects_nonfinite_bundle_before_creating_an_invalid_audit_hash():
    bundle = make_bundle()
    bundle["signal_input"]["nonfinite"] = float("nan")
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert result["blockers"] == ["SIGNAL_BUNDLE_NOT_CANONICALLY_HASHABLE"]
    assert result["audit_events"] == []
    assert result["trade_allowed"] is False

def _persisted_demo_risk_evidence(bundle, *, kill_switch_active=False, binding_override=None):
    request = bundle["permission_request"]
    state = {
        "kill_switch_active": kill_switch_active,
        "updated_at": NOW.isoformat(),
        "updated_by": "pytest-operator",
        "reason": "controlled integration fixture",
        "status": "PERSISTED",
    }
    snapshot = {
        "status": "PASS", "observed_at": NOW.isoformat(), "account_mode": "demo",
        "symbol": request["instrument"], "symbol_group": "fx-major",
        "account_equity": 10000.0, "day_start_equity": 10000.0,
        "equity_high_watermark": 10000.0, "risk_fraction": 0.003,
        "daily_loss_fraction": 0.0, "drawdown_fraction": 0.0,
        "open_positions": 1, "correlated_open_positions": 0,
        "request_binding_sha256": binding_override or risk_request_binding_hash(request),
        "within_high_impact_news_window": False,
    }
    return state, snapshot


def test_persisted_pipeline_permission_requires_global_kill_switch_inactive():
    bundle = make_bundle()
    state, snapshot = _persisted_demo_risk_evidence(bundle, kill_switch_active=True)
    result = evaluate_signal_bundle(
        bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW,
        require_persisted_risk=True, kill_switch_state=state, terminal_risk_snapshot=snapshot,
    )
    assert result["status"] == "DENY"
    assert "KILL_SWITCH_ACTIVE_OR_INVALID" in result["blockers"]
    assert result["trade_allowed"] is False


def test_persisted_pipeline_permission_requires_exact_order_bound_fresh_risk_snapshot():
    bundle = make_bundle()
    state, snapshot = _persisted_demo_risk_evidence(bundle)
    result = evaluate_signal_bundle(
        bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW,
        require_persisted_risk=True, kill_switch_state=state, terminal_risk_snapshot=snapshot,
    )
    assert result["status"] == "REQUIRE_HUMAN"
    permission = result["details"]["permission"]
    assert permission["risk_snapshot_validation"]["status"] == "PASS"
    assert result["trade_allowed"] is False


def test_persisted_pipeline_permission_rejects_snapshot_bound_to_another_order():
    bundle = make_bundle()
    state, snapshot = _persisted_demo_risk_evidence(
        bundle, binding_override="sha256:" + "0" * 64,
    )
    result = evaluate_signal_bundle(
        bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW,
        require_persisted_risk=True, kill_switch_state=state, terminal_risk_snapshot=snapshot,
    )
    assert result["status"] == "DENY"
    assert "RISK_REQUEST_BINDING_MISMATCH" in result["blockers"]
    assert result["trade_allowed"] is False


def test_persisted_pipeline_permission_fails_closed_when_risk_snapshot_missing():
    bundle = make_bundle()
    state, _snapshot = _persisted_demo_risk_evidence(bundle)
    result = evaluate_signal_bundle(
        bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW,
        require_persisted_risk=True, kill_switch_state=state, terminal_risk_snapshot=None,
    )
    assert result["status"] == "DENY"
    assert "RISK_SNAPSHOT_MISSING" in result["blockers"]
    assert result["trade_allowed"] is False


def test_persisted_pipeline_requires_database_backed_source_provenance_before_certainty():
    bundle = make_bundle()
    result = evaluate_signal_bundle(
        bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW,
        require_persisted_sources=True, source_evidence_records=[], source_registry={"publishers": {}},
    )
    assert result["status"] == "GATHER_MORE"
    assert "PERSISTED_SOURCE_PROVENANCE_NOT_VERIFIED" in result["blockers"]
    assert "certainty" not in result["details"]
    assert result["audit_chain_valid"] is True
    assert result["trade_allowed"] is False


def test_persisted_pipeline_requires_database_backed_source_provenance_before_certainty():
    bundle = make_bundle()
    result = evaluate_signal_bundle(
        bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW,
        require_persisted_sources=True, source_evidence_records=[],
        source_registry={"version": "1", "publishers": {}},
    )
    assert result["status"] == "GATHER_MORE"
    assert "PERSISTED_SOURCE_PROVENANCE_NOT_VERIFIED" in result["blockers"]
    assert "certainty" not in result["details"]
    assert result["audit_chain_valid"] is True
    assert result["trade_allowed"] is False


def test_pipeline_rejects_chief_reasoning_bound_to_another_signal_input():
    bundle = make_bundle()
    bundle["chief_reasoning"]["signal_input_hash"] = "sha256:" + "f" * 64
    unsigned = dict(bundle["chief_reasoning"])
    unsigned.pop("reasoning_hash")
    bundle["chief_reasoning"]["reasoning_hash"] = canonical_hash(unsigned)
    bundle["permission_request"]["chief_reasoning_hash"] = bundle["chief_reasoning"]["reasoning_hash"]

    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)

    assert result["status"] == "HOLD"
    assert "CHIEF_SIGNAL_INPUT_HASH_MISMATCH" in result["blockers"]
    assert result["trade_allowed"] is False


def test_pipeline_rejects_chief_reasoning_without_precomputed_hash():
    bundle = make_bundle()
    bundle["chief_reasoning"].pop("reasoning_hash")

    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)

    assert result["status"] == "HOLD"
    assert "CHIEF_REASONING_HASH_MISSING" in result["blockers"]
    assert result["trade_allowed"] is False
