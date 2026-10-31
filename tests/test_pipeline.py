from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from critical_consensus.audit import canonical_hash, verify_audit_chain
from critical_consensus.authorization import InMemoryNonceStore
from critical_consensus.calibration import weighted_consensus
from critical_consensus.evidence import (
    EvidenceError, make_envelope, sign_bundle, sign_decision, sign_review,
)
from critical_consensus.governance import sign_attestation
from critical_consensus.pipeline import (
    evaluate_signal_bundle,
    review_attestation_payload as review_payload,
)
from critical_consensus.risk_controls import risk_request_binding_hash
from jsonschema import Draft202012Validator

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
EVIDENCE_KEY = "e" * 64
GOVERNANCE_KEY = "g" * 64
OPERATOR_KEY = "o" * 64
REVIEW_KEY = "r" * 64
EVIDENCE_ISSUER = "test-ingress"
GOVERNANCE_ISSUER = "calibration-service"
OPERATOR_ISSUER = "operator-console"
REVIEW_ISSUER = "review-service"

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


def sign_review_attestation(bundle, *, now=NOW, key=REVIEW_KEY, issuer=REVIEW_ISSUER):
    signed = dict(bundle)
    signed["review_attestation"] = sign_review(
        review_payload(bundle), key=key, issuer=issuer, issued_at=now,
    )
    return signed


def make_bundle(now=NOW):
    agents = ["PlatformScoutAgent", "TechnicalAnalystAgent", "RiskManagerAgent", "ComplianceAgent"]
    signal_input = {"signal_id": "sig-100", "instrument": "EURUSD", "direction": "BUY",
                    "market_snapshot_hash": "sha256:" + "c" * 64,
                    "available_evidence_refs": ["market-bars:EURUSD:H1","s1","s2","review-evidence-0-0","review-evidence-0-1","review-evidence-1-0","review-evidence-1-1","review-evidence-2-0","review-evidence-2-1","review-evidence-3-0","review-evidence-3-1","resolved-calculation-1","e1","e2","e3","stress-model-1","chief-a","chief-b","chief-c","chief-d","chief-e","chief-f","chief-g","chief-h","chief-i"]}
    signal_input_hash = canonical_hash(signal_input)
    verdicts = [
        {"verdict_id": f"v-{i}", "signal_id": "sig-100", "agent_id": agent,
         "verdict": "APPROVE", "confidence": 0.99,
         "reviewer_count": 999, "created_at": now.isoformat(), "ttl_seconds": 300,
         "input_hash": signal_input_hash, "evidence_refs": ["market-bars:EURUSD:H1"],
         "calibration_meta": {"brier_score": 0.05, "sample_size": 100,
                               "calibration_status": "CALIBRATED"}}
        for i, agent in enumerate(agents)
    ]
    reviews = []
    for i in range(len(verdicts)):
        for j in range(2):
            is_dissent = i == 0 and j == 0
            reviews.append({"review_id": f"rv-{i}-{j}", "target_verdict_id": f"v-{i}",
                            "reviewer_alias": f"reviewer-{j + 1}",
                            "stance": "CHALLENGE" if is_dissent else "CONFIRM",
                            "challenge_type": "LOGIC_GAP" if is_dissent else "NONE",
                            "argument": "A calculation needs reconciliation" if is_dissent else "Independent cross-check supports the evidence",
                            "evidence_refs": [f"review-evidence-{i}-{j}"], "status": "VALIDATED",
                            "created_at": now.isoformat()})
    resolved_challenge = {"challenge_id": "ch-resolved-1", "target_verdict_id": "v-0", "challenge_type": "LOGIC_GAP",
                          "argument": "A calculation needs reconciliation", "rounds": [{"round": 1, "resolved": True}],
                          "status": "RESOLVED", "evidence_refs": ["review-evidence-0-0"],
                          "resolution_evidence_refs": ["resolved-calculation-1"],
                          "created_at": now.isoformat()}
    challenge_snapshot = [resolved_challenge]
    bundle = {
        "signal_id": "sig-100", "signal_input": signal_input,
        "signal_input_hash": signal_input_hash, "verdicts": verdicts, "peer_reviews": reviews,
        "open_challenges": [], "challenge_records": challenge_snapshot, "challenge_resolution_status": "RESOLVED",
        "contradictions_last_n_hours": 0, "contradiction_lookback_hours": 6,
        "news_or_rumor": False,
        "key_sources": [
            {"source_id": "s1", "credibility": 0.95, "age_seconds": 20, "ttl_seconds": 900,
             "observed_at": (now - timedelta(seconds=20)).isoformat()},
            {"source_id": "s2", "credibility": 0.94, "age_seconds": 40, "ttl_seconds": 900,
             "observed_at": (now - timedelta(seconds=40)).isoformat()},
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
            "reasoning_id": "chief-r-100", "signal_id": "sig-100", "created_at": now.isoformat(),
            "verdict": "CONFIRM",
            "steps": {step: {"passed": True, "detail": f"checked {step}", "evidence_refs": [f"chief-{step}"]}
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
            "expires_at": (now + timedelta(minutes=5)).isoformat(), "within_high_impact_news_window": False,
            "risk_snapshot": {"risk_fraction": 0.003, "daily_loss_fraction": 0.004, "drawdown_fraction": 0.02,
                              "open_stop_risk_fraction": 0.0,
                              "open_positions": 1, "correlated_open_positions": 0,
                              "account_login": 5000001, "account_server": "Demo-Server",
                              "position_symbols": ["EURUSD"]},
        },
    }
    bundle["chief_reasoning"]["reasoning_hash"] = canonical_hash(bundle["chief_reasoning"])
    bundle["permission_request"]["chief_reasoning_hash"] = bundle["chief_reasoning"]["reasoning_hash"]
    bundle["permission_request"]["consensus_score"] = weighted_consensus(verdicts)
    return sign_review_attestation(bundle, now=now)


def make_governance_attestation(now=NOW, *, dissent_rate=0.125, false_certainty_rate=0.02,
                                sample_size=80, agent_calibration=None):
    payload = {
        "source": "calibration-service", "observed_at": now.isoformat(),
        "window_start": (now - timedelta(days=7)).isoformat(),
        "window_end": (now - timedelta(minutes=5)).isoformat(),
        "dissent_rate": dissent_rate, "dissent_sample_size": sample_size,
        "false_certainty_rate": false_certainty_rate,
        "false_certainty_sample_size": sample_size,
        "method_version": "brier-shrink-v1",
        "agent_calibration": agent_calibration if agent_calibration is not None else {
            agent: {"brier_score": 0.05, "sample_size": 100, "calibration_status": "CALIBRATED"}
            for agent in CERTAINTY_POLICY["mandatory_agents"]
        },
    }
    return sign_attestation(payload, key=GOVERNANCE_KEY, issuer=GOVERNANCE_ISSUER, issued_at=now)


def sign_operator_approval(bundle, *, now=NOW, key=OPERATOR_KEY, issuer=OPERATOR_ISSUER,
                           risk_snapshot=None):
    """Attach a correctly bound operator signature to a bundle's user_decision."""
    signed = dict(bundle)
    decision = dict(bundle["user_decision"])
    decision.setdefault("signal_id", bundle.get("signal_id"))
    decision.setdefault("chief_reasoning_hash",
                        (bundle.get("chief_reasoning") or {}).get("reasoning_hash"))
    permission_request = bundle.get("permission_request")
    decision.setdefault("permission_request_sha256",
                        canonical_hash(dict(permission_request))
                        if isinstance(permission_request, dict) else None)
    if isinstance(risk_snapshot, dict):
        decision.setdefault("risk_snapshot_sha256", canonical_hash(dict(risk_snapshot)))
    signed["user_decision"] = decision
    signed["user_decision_envelope"] = sign_decision(
        decision, key=key, issuer=issuer, issued_at=now,
    )
    return signed


def run_pipeline(bundle, *, now=NOW, sign=True, policy=None, resign_review=None, **kwargs):
    body = dict(bundle)
    if body.get("user_decision") is not None and "user_decision_envelope" not in body:
        body = sign_operator_approval(
            body, now=now, risk_snapshot=kwargs.get("terminal_risk_snapshot"))
    body.setdefault("governance_attestation", make_governance_attestation(now))
    if resign_review is None:
        # Tests mutate make_bundle() output as if the review service produced that exact
        # snapshot; re-attesting keeps the signed review view consistent with the bundle.
        resign_review = sign
    if resign_review:
        body = sign_review_attestation(body, now=now)
    if sign:
        try:
            body = sign_bundle(body, key=EVIDENCE_KEY, issuer=EVIDENCE_ISSUER, issued_at=now)
        except (EvidenceError, TypeError, ValueError):
            # Non-serializable inputs must reach the pipeline's canonical-hash rejection.
            body = bundle
    kwargs.setdefault("evidence_key", EVIDENCE_KEY)
    kwargs.setdefault("governance_key", GOVERNANCE_KEY)
    kwargs.setdefault("expected_evidence_issuer", EVIDENCE_ISSUER)
    kwargs.setdefault("operator_key", OPERATOR_KEY)
    kwargs.setdefault("expected_operator_issuer", OPERATOR_ISSUER)
    kwargs.setdefault("review_key", REVIEW_KEY)
    kwargs.setdefault("expected_review_issuer", REVIEW_ISSUER)
    return evaluate_signal_bundle(body, policy or CERTAINTY_POLICY, PERMISSION_MATRIX,
                                  now=now, **kwargs)


def test_pipeline_reaches_human_gate_but_does_not_create_order():
    bundle = make_bundle()
    root = Path(__file__).resolve().parents[1]
    chief_schema = json.loads((root / "schemas" / "chief_reasoning.schema.json").read_text(encoding="utf-8"))
    permission_schema = json.loads((root / "schemas" / "permission_request.schema.json").read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(chief_schema).iter_errors(bundle["chief_reasoning"]))
    permission_contract = dict(bundle["permission_request"], chief_verdict="CONFIRM", certainty_status="PASS",
                               compliance_status="PASS", risk_flags=[])
    assert not list(Draft202012Validator(permission_schema).iter_errors(permission_contract))
    result = run_pipeline(bundle)
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
    result = run_pipeline(bundle)
    assert result["status"] == "GATHER_MORE"
    assert any("INSUFFICIENT_OR_INVALID_PEER_REVIEWS" in x for x in result["blockers"])


def test_pipeline_open_challenge_blocks_certainty_gate():
    bundle = make_bundle()
    bundle["peer_reviews"][0] = {"target_verdict_id": "v-0", "reviewer_alias": "reviewer-1",
        "stance": "CHALLENGE", "challenge_type": "LOGIC_GAP", "argument": "The calculation is inconsistent",
        "evidence_refs": ["review-evidence-0-0"], "status": "VALIDATED"}
    bundle["challenge_records"] = [{"challenge_id": "ch-1", "target_verdict_id": "v-0", "challenge_type": "LOGIC_GAP",
                                     "status": "OPEN", "evidence_refs": ["calc-contradiction"]}]
    result = run_pipeline(bundle)
    assert result["status"] == "GATHER_MORE"
    assert any("OPEN_CHALLENGES_PRESENT" in x for x in result["blockers"])


def test_pipeline_rejects_tampered_chief_reasoning_hash():
    bundle = make_bundle()
    bundle["chief_reasoning"]["reasoning_hash"] = "sha256:forged-value-which-is-long-enough"
    result = run_pipeline(bundle)
    assert result["status"] == "HOLD"
    assert result["blockers"] == ["CHIEF_REASONING_HASH_MISMATCH"]


def test_pipeline_live_mode_denied_by_policy():
    bundle = make_bundle()
    bundle["permission_request"]["mode"] = "live"
    result = run_pipeline(bundle)
    assert result["status"] == "DENY"
    assert "LIVE_MODE_DISABLED" in result["blockers"]


def test_pipeline_valid_human_approval_is_only_authorized_once():
    bundle = make_bundle()
    bundle["user_decision"] = {"decision": "APPROVE", "user_id": "operator-1", "reason": "Reviewed dissent and tail risk",
                               "permission_id": "perm-1", "decision_id": "decision-1",
                               "nonce": "n" * 24, "timestamp": NOW.isoformat(), "ttl_seconds": 300}
    state, snapshot = _persisted_demo_risk_evidence(bundle)
    result = run_pipeline(
        bundle, require_persisted_risk=True, kill_switch_state=state,
        terminal_risk_snapshot=snapshot, authorization_store=InMemoryNonceStore(),
    )
    assert result["status"] == "AUTHORIZED_ONCE"
    assert result["details"]["authorization"]["authorized"] is True
    assert result["trade_allowed"] is False
    assert "order_command" not in result


def test_pipeline_missing_challenge_ledger_fails_closed():
    bundle = make_bundle()
    bundle.pop("challenge_records")
    result = run_pipeline(bundle)
    assert result["status"] == "GATHER_MORE"
    assert any("CHALLENGE_LEDGER_MISSING" in x for x in result["blockers"])
    assert result["audit_chain_valid"] is True


def test_pipeline_rejects_permission_request_chief_hash_mismatch():
    bundle = make_bundle()
    bundle["permission_request"]["chief_reasoning_hash"] = "sha256:" + "0" * 64
    result = run_pipeline(bundle)
    assert result["status"] == "DENY"
    assert "PERMISSION_CHIEF_REASONING_HASH_MISMATCH" in result["blockers"]


def test_pipeline_rejects_permission_request_consensus_mismatch():
    bundle = make_bundle()
    bundle["permission_request"]["consensus_score"] = 0.1
    result = run_pipeline(bundle)
    assert result["status"] == "DENY"
    assert "PERMISSION_CONSENSUS_SCORE_MISMATCH" in result["blockers"]


def test_pipeline_human_packet_contains_full_challenge_ledger_and_sources():
    result = run_pipeline(make_bundle())
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
    result = run_pipeline(bundle)
    assert result["status"] == "HOLD"
    assert result["blockers"] == ["CHIEF_SIGNAL_ID_MISMATCH"]


def test_pipeline_rejects_permission_source_summary_tampering():
    bundle = make_bundle()
    bundle["permission_request"]["sources"][0]["credibility"] = 0.99
    result = run_pipeline(bundle)
    assert result["status"] == "DENY"
    assert "PERMISSION_SOURCE_SUMMARY_MISMATCH" in result["blockers"]


def test_verdict_for_another_signal_is_rejected_before_consensus():
    bundle = make_bundle()
    bundle["verdicts"][0]["signal_id"] = "different-signal"
    result = run_pipeline(bundle)
    assert result["status"] == "GATHER_MORE"
    assert result["trade_allowed"] is False
    assert "VERDICT_0:VERDICT_SIGNAL_ID_MISMATCH" in result["blockers"]
    assert "certainty" not in result["details"]


def test_expired_verdict_is_rejected_before_consensus():
    bundle = make_bundle()
    bundle["verdicts"][0]["created_at"] = (NOW - timedelta(seconds=301)).isoformat()
    bundle["verdicts"][0]["ttl_seconds"] = 300
    result = run_pipeline(bundle)
    assert result["status"] == "GATHER_MORE"
    assert "VERDICT_0:VERDICT_EXPIRED" in result["blockers"]
    assert result["trade_allowed"] is False


def test_verdict_with_future_timestamp_beyond_clock_skew_is_rejected():
    bundle = make_bundle()
    bundle["verdicts"][0]["created_at"] = (NOW + timedelta(seconds=31)).isoformat()
    result = run_pipeline(bundle)
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
        result = run_pipeline(bundle)
        assert result["status"] == "GATHER_MORE"
        assert f"VERDICT_0:{expected_blocker}" in result["blockers"]
        assert result["trade_allowed"] is False


def test_verdict_at_exact_ttl_boundary_is_accepted():
    bundle = make_bundle()
    bundle["verdicts"][0]["created_at"] = (NOW - timedelta(seconds=300)).isoformat()
    result = run_pipeline(bundle)
    assert result["status"] == "REQUIRE_HUMAN"
    assert result["details"]["certainty"]["status"] == "PASS"
    assert result["trade_allowed"] is False
    assert "order_command" not in result


def test_pipeline_rejects_tampered_signal_input_hash_before_consensus():
    bundle = make_bundle()
    bundle["signal_input_hash"] = "sha256:" + "0" * 64
    result = run_pipeline(bundle)
    assert result["status"] == "GATHER_MORE"
    assert result["blockers"] == ["SIGNAL_INPUT_HASH_MISMATCH"]
    assert "certainty" not in result["details"]
    assert result["audit_chain_valid"] is True


def test_pipeline_binds_every_agent_verdict_to_the_same_input_snapshot():
    bundle = make_bundle()
    bundle["verdicts"][0]["input_hash"] = "sha256:" + "f" * 64
    result = run_pipeline(bundle)
    assert result["status"] == "GATHER_MORE"
    assert "VERDICT_0:VERDICT_INPUT_HASH_DOES_NOT_MATCH_SIGNAL_INPUT" in result["blockers"]
    assert "certainty" not in result["details"]


def test_pipeline_rejects_nonfinite_bundle_before_creating_an_invalid_audit_hash():
    bundle = make_bundle()
    bundle["signal_input"]["nonfinite"] = float("nan")
    result = run_pipeline(bundle)
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
        "account_login": 5000001, "account_server": "Demo-Server",
        "symbol": request["instrument"], "symbol_group": "fx-major",
        "account_equity": 10000.0, "account_balance": 10000.0, "daily_pnl": 0.0,
        "day_start_equity": 10000.0,
        "equity_high_watermark": 10000.0, "risk_fraction": 0.003,
        "risk_value_account_currency": 30.0,
        "risk_tick_size": 0.0001, "risk_tick_value_loss": 15.0,
        "reference_price": request["expected_price"],
        "daily_loss_fraction": 0.0, "drawdown_fraction": 0.0,
        "open_stop_risk_fraction": 0.0,
        "open_positions": 1, "correlated_open_positions": 0,
        "position_symbols": [request["instrument"]],
        "request_binding_sha256": binding_override or risk_request_binding_hash(request),
        "within_high_impact_news_window": False,
    }
    return state, snapshot


def test_persisted_kill_switch_denies_before_incomplete_bundle_is_requested():
    result = run_pipeline(
        {},
        require_persisted_risk=True,
        kill_switch_state={
            "kill_switch_active": True,
            "status": "PERSISTED",
            "updated_at": NOW.isoformat(),
            "updated_by": "pytest-operator",
            "reason": "emergency stop",
        },
    )
    assert result["status"] == "DENY"
    assert "KILL_SWITCH_ACTIVE_OR_INVALID" in result["blockers"]
    assert result["trade_allowed"] is False
    assert result["audit_chain_valid"] is True


def test_missing_persisted_kill_switch_denies_before_bundle_validation():
    result = run_pipeline({}, require_persisted_risk=True, kill_switch_state=None)
    assert result["status"] == "DENY"
    assert "KILL_SWITCH_STATE_UNKNOWN" in result["blockers"]
    assert result["trade_allowed"] is False
    assert result["audit_chain_valid"] is True


def test_persisted_pipeline_permission_requires_global_kill_switch_inactive():
    bundle = make_bundle()
    state, snapshot = _persisted_demo_risk_evidence(bundle, kill_switch_active=True)
    result = run_pipeline(
        bundle, require_persisted_risk=True, kill_switch_state=state,
        terminal_risk_snapshot=snapshot,
    )
    assert result["status"] == "DENY"
    assert "KILL_SWITCH_ACTIVE_OR_INVALID" in result["blockers"]
    assert result["trade_allowed"] is False


def test_persisted_pipeline_permission_requires_exact_order_bound_fresh_risk_snapshot():
    bundle = make_bundle()
    state, snapshot = _persisted_demo_risk_evidence(bundle)
    result = run_pipeline(
        bundle, require_persisted_risk=True, kill_switch_state=state,
        terminal_risk_snapshot=snapshot,
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
    result = run_pipeline(
        bundle, require_persisted_risk=True, kill_switch_state=state,
        terminal_risk_snapshot=snapshot,
    )
    assert result["status"] == "DENY"
    assert "RISK_REQUEST_BINDING_MISMATCH" in result["blockers"]
    assert result["trade_allowed"] is False


def test_persisted_pipeline_permission_fails_closed_when_risk_snapshot_missing():
    bundle = make_bundle()
    state, _snapshot = _persisted_demo_risk_evidence(bundle)
    result = run_pipeline(
        bundle, require_persisted_risk=True, kill_switch_state=state,
        terminal_risk_snapshot=None,
    )
    assert result["status"] == "DENY"
    assert "RISK_SNAPSHOT_MISSING" in result["blockers"]
    assert result["trade_allowed"] is False


def test_pipeline_requires_a_valid_signed_evidence_envelope():
    bundle = make_bundle()
    result = evaluate_signal_bundle(
        bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW,
        evidence_key=EVIDENCE_KEY, governance_key=GOVERNANCE_KEY,
        expected_evidence_issuer=EVIDENCE_ISSUER,
        review_key=REVIEW_KEY, expected_review_issuer=REVIEW_ISSUER,
    )
    assert result["status"] == "GATHER_MORE"
    assert "EVIDENCE_ENVELOPE_MISSING_OR_INVALID" in result["blockers"]
    assert "certainty" not in result["details"]


def test_pipeline_requires_configured_issuer_pinning():
    result = evaluate_signal_bundle(
        make_bundle(), CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW,
        evidence_key=EVIDENCE_KEY, governance_key=GOVERNANCE_KEY,
        review_key=REVIEW_KEY, expected_review_issuer=REVIEW_ISSUER,
    )
    assert result["status"] == "GATHER_MORE"
    assert result["blockers"] == ["EVIDENCE_EXPECTED_ISSUER_NOT_CONFIGURED"]


def test_pipeline_rejects_envelope_signed_by_unexpected_issuer():
    body = sign_bundle({**make_bundle(), "governance_attestation": make_governance_attestation()},
                       key=EVIDENCE_KEY, issuer="rogue-ingress", issued_at=NOW)
    result = run_pipeline(body, sign=False)
    assert result["status"] == "GATHER_MORE"
    assert "EVIDENCE_ISSUER_MISMATCH" in result["blockers"]


def test_pipeline_rejects_shared_evidence_and_governance_key():
    result = run_pipeline(make_bundle(), governance_key=EVIDENCE_KEY)
    assert result["status"] == "GATHER_MORE"
    assert "GOVERNANCE_KEY_NOT_DISTINCT_FROM_EVIDENCE_KEY" in result["blockers"]


def test_pipeline_rejects_bundle_tampering_after_signing():
    bundle = sign_bundle({**make_bundle(),
                          "governance_attestation": make_governance_attestation()},
                         key=EVIDENCE_KEY, issuer=EVIDENCE_ISSUER, issued_at=NOW)
    bundle["verdicts"][0]["verdict"] = "VETO"
    result = evaluate_signal_bundle(
        bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW,
        evidence_key=EVIDENCE_KEY, governance_key=GOVERNANCE_KEY,
        expected_evidence_issuer=EVIDENCE_ISSUER,
        review_key=REVIEW_KEY, expected_review_issuer=REVIEW_ISSUER,
    )
    assert result["status"] == "GATHER_MORE"
    assert "EVIDENCE_PAYLOAD_HASH_MISMATCH" in result["blockers"]


def test_pipeline_requires_a_valid_governance_attestation():
    bundle = make_bundle()
    body = sign_bundle({**bundle, "governance_attestation": {"payload": {}, "envelope": {}}},
                       key=EVIDENCE_KEY, issuer=EVIDENCE_ISSUER, issued_at=NOW)
    result = evaluate_signal_bundle(
        body, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW,
        evidence_key=EVIDENCE_KEY, governance_key=GOVERNANCE_KEY,
        expected_evidence_issuer=EVIDENCE_ISSUER,
        review_key=REVIEW_KEY, expected_review_issuer=REVIEW_ISSUER,
    )
    assert result["status"] == "GATHER_MORE"
    assert any(blocker.startswith("GOVERNANCE_") for blocker in result["blockers"])


def test_compliance_status_is_derived_from_the_compliance_agent_not_the_bundle_field():
    without_field = make_bundle()
    without_field.pop("compliance_status")
    assert run_pipeline(without_field)["status"] == "REQUIRE_HUMAN"

    forged_field = make_bundle()
    forged_field["compliance_status"] = "FAIL"
    assert run_pipeline(forged_field)["status"] == "REQUIRE_HUMAN"

    # Decisive direction: the bundle claims PASS, but the policy-designated
    # compliance agent has no verdict at all. Only a derived status can catch this.
    policy = dict(CERTAINTY_POLICY)
    policy["compliance_agent_id"] = "LegalAgent"
    result = run_pipeline(make_bundle(), policy=policy)
    assert result["status"] == "DENY"
    assert "COMPLIANCE_NOT_PASSED" in result["blockers"]


def test_permission_request_must_match_signal_instrument_and_direction():
    bundle = make_bundle()
    bundle["permission_request"]["instrument"] = "GBPUSD"
    bundle["permission_request"]["direction"] = "SELL"
    result = run_pipeline(bundle)
    assert result["status"] == "DENY"
    assert "PERMISSION_INSTRUMENT_SIGNAL_MISMATCH" in result["blockers"]
    assert "PERMISSION_DIRECTION_SIGNAL_MISMATCH" in result["blockers"]


def test_signal_input_direction_is_required():
    bundle = make_bundle()
    bundle["signal_input"].pop("direction")
    bundle["signal_input_hash"] = canonical_hash(bundle["signal_input"])
    for verdict in bundle["verdicts"]:
        verdict["input_hash"] = bundle["signal_input_hash"]
    result = run_pipeline(bundle)
    assert result["status"] == "GATHER_MORE"
    assert "SIGNAL_INPUT_DIRECTION_INVALID" in result["blockers"]


def test_replayed_authorization_nonce_is_denied():
    store = InMemoryNonceStore()
    bundle = make_bundle()
    bundle["user_decision"] = {"decision": "APPROVE", "user_id": "operator-1",
                               "reason": "Reviewed dissent and tail risk", "permission_id": "perm-1",
                               "decision_id": "decision-1", "nonce": "n" * 24,
                               "timestamp": NOW.isoformat(), "ttl_seconds": 300}
    state, snapshot = _persisted_demo_risk_evidence(bundle)
    kwargs = {"require_persisted_risk": True, "kill_switch_state": state,
              "terminal_risk_snapshot": snapshot, "authorization_store": store}
    first = run_pipeline(bundle, **kwargs)
    second = run_pipeline(bundle, **kwargs)
    assert first["status"] == "AUTHORIZED_ONCE"
    assert second["status"] == "DENY"
    assert "AUTHORIZATION_NONCE_REPLAYED" in second["blockers"]


def test_renaming_the_permission_cannot_reset_the_authorization_tombstone():
    store = InMemoryNonceStore()
    bundle = _approval_bundle()
    state, snapshot = _persisted_demo_risk_evidence(bundle)
    first = run_pipeline(
        bundle, require_persisted_risk=True, kill_switch_state=state,
        terminal_risk_snapshot=snapshot, authorization_store=store,
    )
    assert first["status"] == "AUTHORIZED_ONCE"

    renamed = _approval_bundle()
    renamed["permission_request"]["permission_id"] = "perm-2"
    renamed["user_decision"]["permission_id"] = "perm-2"
    renamed["user_decision"]["nonce"] = "m" * 24
    renamed_state, renamed_snapshot = _persisted_demo_risk_evidence(renamed)
    second = run_pipeline(
        renamed, require_persisted_risk=True, kill_switch_state=renamed_state,
        terminal_risk_snapshot=renamed_snapshot, authorization_store=store,
    )
    assert second["status"] == "DENY"
    assert "AUTHORIZATION_PERMISSION_ALREADY_USED" in second["blockers"]


def test_operator_approval_is_bound_to_the_validated_risk_snapshot():
    bundle = _approval_bundle()
    state, snapshot = _persisted_demo_risk_evidence(bundle)
    bundle = sign_operator_approval(bundle, risk_snapshot=snapshot)
    bundle["user_decision"] = dict(
        bundle["user_decision"],
        risk_snapshot_sha256=canonical_hash({**snapshot, "risk_fraction": 0.0001}))
    bundle["user_decision_envelope"] = sign_decision(
        bundle["user_decision"], key=OPERATOR_KEY, issuer=OPERATOR_ISSUER, issued_at=NOW)
    result = run_pipeline(
        bundle, require_persisted_risk=True, kill_switch_state=state,
        terminal_risk_snapshot=snapshot, authorization_store=InMemoryNonceStore(),
    )
    assert result["status"] == "DENY"
    assert "USER_DECISION_RISK_SNAPSHOT_MISMATCH" in result["blockers"]


def test_authorization_without_persisted_risk_evidence_is_denied():
    bundle = make_bundle()
    bundle["user_decision"] = {"decision": "APPROVE", "user_id": "operator-1",
                               "reason": "Reviewed dissent and tail risk", "permission_id": "perm-1",
                               "decision_id": "decision-1", "nonce": "n" * 24,
                               "timestamp": NOW.isoformat(), "ttl_seconds": 300}
    result = run_pipeline(bundle, authorization_store=InMemoryNonceStore())
    assert result["status"] == "DENY"
    assert "PERSISTED_RISK_REQUIRED_FOR_AUTHORIZATION" in result["blockers"]


def test_self_declared_calibration_is_replaced_by_the_attested_report():
    bundle = make_bundle()
    for verdict in bundle["verdicts"]:
        verdict["calibration_meta"] = {"brier_score": 0.0, "sample_size": 10**6,
                                       "calibration_status": "CALIBRATED"}
    assert run_pipeline(bundle)["status"] == "REQUIRE_HUMAN"

    bad_bundle = make_bundle()
    bad_bundle["governance_attestation"] = make_governance_attestation(
        agent_calibration={agent: {"brier_score": 0.5, "sample_size": 100,
                                    "calibration_status": "CALIBRATED"}
                           for agent in CERTAINTY_POLICY["mandatory_agents"]},
    )
    bad = run_pipeline(bad_bundle)
    assert bad["status"] == "GATHER_MORE"
    assert any("CALIBRATION" in blocker for blocker in bad["blockers"])


def test_expired_peer_review_fails_closed():
    bundle = make_bundle()
    bundle["peer_reviews"][0]["created_at"] = (NOW - timedelta(hours=2)).isoformat()
    result = run_pipeline(bundle)
    assert result["status"] == "GATHER_MORE"
    assert any("INVALID_PEER_REVIEW_RECORD" in blocker for blocker in result["blockers"])
    assert result["trade_allowed"] is False


def test_expired_challenge_record_fails_closed():
    bundle = make_bundle()
    bundle["challenge_records"][0]["created_at"] = (NOW - timedelta(hours=2)).isoformat()
    result = run_pipeline(bundle)
    assert result["status"] == "GATHER_MORE"
    assert any("CHALLENGE_0_CREATED_AT_INVALID" in blocker for blocker in result["blockers"])
    assert result["trade_allowed"] is False


def _approval_bundle():
    bundle = make_bundle()
    bundle["user_decision"] = {"decision": "APPROVE", "user_id": "operator-1",
                               "reason": "Reviewed dissent and tail risk",
                               "permission_id": "perm-1", "decision_id": "decision-1",
                               "nonce": "n" * 24, "timestamp": NOW.isoformat(),
                               "ttl_seconds": 300}
    return bundle


def _evaluate_with_approval(bundle, **kwargs):
    body = dict(bundle)
    body.setdefault("governance_attestation", make_governance_attestation())
    body = sign_bundle(body, key=EVIDENCE_KEY, issuer=EVIDENCE_ISSUER, issued_at=NOW)
    args = dict(evidence_key=EVIDENCE_KEY, governance_key=GOVERNANCE_KEY,
                expected_evidence_issuer=EVIDENCE_ISSUER,
                operator_key=OPERATOR_KEY, expected_operator_issuer=OPERATOR_ISSUER,
                review_key=REVIEW_KEY, expected_review_issuer=REVIEW_ISSUER)
    args.update(kwargs)
    return evaluate_signal_bundle(body, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW, **args)


def test_evidence_signed_bundle_cannot_contain_an_unsigned_approval():
    result = _evaluate_with_approval(_approval_bundle())
    assert result["status"] == "DENY"
    assert "EVIDENCE_ENVELOPE_MISSING_OR_INVALID" in result["blockers"]
    assert result["trade_allowed"] is False


def test_approval_signed_in_the_evidence_domain_is_rejected():
    bundle = _approval_bundle()
    decision = bundle["user_decision"]
    bundle["user_decision_envelope"] = make_envelope(
        decision, key=OPERATOR_KEY, issuer=OPERATOR_ISSUER, issued_at=NOW)
    result = _evaluate_with_approval(bundle)
    assert result["status"] == "DENY"
    assert "EVIDENCE_SIGNATURE_MISMATCH" in result["blockers"]


def test_approval_tampering_after_signing_is_rejected():
    bundle = sign_operator_approval(_approval_bundle())
    bundle["user_decision"] = dict(bundle["user_decision"], decision="REJECT")
    result = run_pipeline(bundle)
    assert result["status"] == "DENY"
    assert "EVIDENCE_PAYLOAD_HASH_MISMATCH" in result["blockers"]


def test_operator_key_and_issuer_must_be_distinct_signing_domains():
    state, snapshot = _persisted_demo_risk_evidence(_approval_bundle())
    shared_key = run_pipeline(
        _approval_bundle(), require_persisted_risk=True, kill_switch_state=state,
        terminal_risk_snapshot=snapshot, authorization_store=InMemoryNonceStore(),
        operator_key=EVIDENCE_KEY,
    )
    assert shared_key["status"] == "DENY"
    assert "OPERATOR_KEY_NOT_DISTINCT" in shared_key["blockers"]
    shared_issuer = run_pipeline(
        _approval_bundle(), require_persisted_risk=True, kill_switch_state=state,
        terminal_risk_snapshot=snapshot, authorization_store=InMemoryNonceStore(),
        expected_operator_issuer=EVIDENCE_ISSUER,
    )
    assert shared_issuer["status"] == "DENY"
    assert "OPERATOR_ISSUER_NOT_DISTINCT" in shared_issuer["blockers"]


def test_approval_must_bind_the_signal_and_reviewed_chief_reasoning():
    bundle = _approval_bundle()
    decision = bundle["user_decision"]
    bundle["user_decision_envelope"] = sign_decision(
        decision, key=OPERATOR_KEY, issuer=OPERATOR_ISSUER, issued_at=NOW)
    state, snapshot = _persisted_demo_risk_evidence(bundle)
    result = run_pipeline(bundle, require_persisted_risk=True, kill_switch_state=state,
                          terminal_risk_snapshot=snapshot,
                          authorization_store=InMemoryNonceStore())
    assert result["status"] == "DENY"
    assert "USER_DECISION_SIGNAL_ID_MISMATCH" in result["blockers"]
    assert "USER_DECISION_CHIEF_HASH_MISMATCH" in result["blockers"]


def test_risk_snapshot_with_forged_aggregates_is_denied_at_authorization():
    bundle = make_bundle()
    bundle["user_decision"] = {"decision": "APPROVE", "user_id": "operator-1",
                               "reason": "Reviewed dissent and tail risk",
                               "permission_id": "perm-1", "decision_id": "decision-1",
                               "nonce": "n" * 24, "timestamp": NOW.isoformat(),
                               "ttl_seconds": 300}
    state, snapshot = _persisted_demo_risk_evidence(bundle)
    snapshot["risk_fraction"] = 0.0001  # reported risk no longer matches size/SL/equity
    result = run_pipeline(bundle, require_persisted_risk=True, kill_switch_state=state,
                          terminal_risk_snapshot=snapshot,
                          authorization_store=InMemoryNonceStore())
    assert result["status"] == "DENY"
    assert "RISK_FRACTION_DOES_NOT_MATCH_ORDER" in result["blockers"]


def test_operator_approval_is_bound_to_the_exact_order_parameters():
    bundle = make_bundle()
    bundle["user_decision"] = {"decision": "APPROVE", "user_id": "operator-1",
                               "reason": "Reviewed dissent and tail risk", "permission_id": "perm-1",
                               "decision_id": "decision-1", "nonce": "n" * 24,
                               "timestamp": NOW.isoformat(), "ttl_seconds": 300}
    approved = sign_operator_approval(bundle)
    # An evidence-key holder redirects the approval to a different permission after signing.
    approved["permission_request"] = {**approved["permission_request"], "permission_id": "perm-2"}
    state, snapshot = _persisted_demo_risk_evidence(approved)
    body = sign_bundle({**approved, "governance_attestation": make_governance_attestation()},
                       key=EVIDENCE_KEY, issuer=EVIDENCE_ISSUER, issued_at=NOW)
    result = evaluate_signal_bundle(
        body, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW,
        evidence_key=EVIDENCE_KEY, governance_key=GOVERNANCE_KEY,
        expected_evidence_issuer=EVIDENCE_ISSUER, operator_key=OPERATOR_KEY,
        expected_operator_issuer=OPERATOR_ISSUER,
        review_key=REVIEW_KEY, expected_review_issuer=REVIEW_ISSUER,
        require_persisted_risk=True,
        kill_switch_state=state, terminal_risk_snapshot=snapshot,
        authorization_store=InMemoryNonceStore(),
    )
    assert result["status"] == "DENY"
    assert "USER_DECISION_PERMISSION_HASH_MISMATCH" in result["blockers"]
    assert result["trade_allowed"] is False


def test_chief_challenge_ledger_snapshot_must_match_the_bundle_ledger():
    bundle = make_bundle()
    forged = dict(bundle["challenge_records"][0], challenge_id="ch-forged")
    bundle["chief_reasoning"]["challenge_ledger_snapshot"] = [forged]
    bundle["chief_reasoning"]["reviewed_challenge_ids"] = ["ch-forged"]
    bundle["chief_reasoning"]["challenge_ledger_hash"] = canonical_hash([forged])
    bundle["chief_reasoning"]["reasoning_hash"] = canonical_hash(bundle["chief_reasoning"])
    bundle["permission_request"]["chief_reasoning_hash"] = bundle["chief_reasoning"]["reasoning_hash"]
    result = run_pipeline(bundle)
    assert result["status"] == "HOLD"
    assert "CHIEF_CHALLENGE_LEDGER_SNAPSHOT_MISMATCH" in result["blockers"]
    assert result["trade_allowed"] is False


def test_challenge_resolution_must_cite_evidence_not_already_in_the_challenge():
    bundle = make_bundle()
    bundle["challenge_records"][0]["resolution_evidence_refs"] = ["review-evidence-0-0"]
    result = run_pipeline(bundle)
    assert result["status"] == "GATHER_MORE"
    assert any("CHALLENGE_RESOLUTION_MISSING_NEW_EVIDENCE" in blocker
               for blocker in result["blockers"])
    assert result["trade_allowed"] is False


def test_operator_key_must_differ_from_the_governance_domain():
    state, snapshot = _persisted_demo_risk_evidence(_approval_bundle())
    for kwargs, blocker in (
        ({"operator_key": GOVERNANCE_KEY}, "OPERATOR_KEY_NOT_DISTINCT"),
        ({"expected_operator_issuer": GOVERNANCE_ISSUER}, "OPERATOR_ISSUER_NOT_DISTINCT"),
    ):
        result = run_pipeline(
            _approval_bundle(), require_persisted_risk=True, kill_switch_state=state,
            terminal_risk_snapshot=snapshot, authorization_store=InMemoryNonceStore(), **kwargs,
        )
        assert result["status"] == "DENY"
        assert blocker in result["blockers"]


def test_human_review_packet_shows_the_terminal_snapshot_the_gate_validated():
    bundle = make_bundle()
    state, snapshot = _persisted_demo_risk_evidence(bundle)
    # The bundle's own summary understates risk; the packet must not echo it.
    bundle["permission_request"]["risk_snapshot"]["risk_fraction"] = 0.0001
    result = run_pipeline(bundle, require_persisted_risk=True, kill_switch_state=state,
                          terminal_risk_snapshot=snapshot)
    assert result["status"] == "REQUIRE_HUMAN"
    packet = result["details"]["human_review_packet"]
    assert packet["risk_snapshot_source"] == "persisted_terminal_snapshot"
    assert packet["risk_snapshot"]["risk_fraction"] == snapshot["risk_fraction"]
    assert packet["risk_snapshot"]["open_stop_risk_fraction"] == 0.0
    assert packet["risk_snapshot_validation"]["status"] == "PASS"
    assert packet["risk_reconciliation"]["status"] == "PASS"


def test_pipeline_requires_a_signed_review_attestation():
    bundle = make_bundle()
    bundle.pop("review_attestation")
    result = run_pipeline(bundle, resign_review=False)
    assert result["status"] == "GATHER_MORE"
    assert "REVIEW_ENVELOPE_MISSING_OR_INVALID" in result["blockers"]
    assert "certainty" not in result["details"]


def test_evidence_key_holder_cannot_mint_chief_or_peer_review_output():
    bundle = make_bundle()
    bundle["chief_reasoning"]["tail_risk_probability"] = 0.0001
    bundle["chief_reasoning"]["reasoning_hash"] = canonical_hash(bundle["chief_reasoning"])
    bundle["permission_request"]["chief_reasoning_hash"] = bundle["chief_reasoning"]["reasoning_hash"]
    # The evidence key can re-sign the whole bundle, but not the review-domain attestation.
    result = run_pipeline(bundle, resign_review=False)
    assert result["status"] == "GATHER_MORE"
    assert "REVIEW_PAYLOAD_HASH_MISMATCH" in result["blockers"]
    assert "certainty" not in result["details"]


def test_review_attestation_is_bound_to_the_signal_id():
    bundle = make_bundle()
    bundle["review_attestation"] = sign_review(
        {**review_payload(bundle), "signal_id": "sig-other"},
        key=REVIEW_KEY, issuer=REVIEW_ISSUER, issued_at=NOW,
    )
    result = run_pipeline(bundle, resign_review=False)
    assert result["status"] == "GATHER_MORE"
    assert "REVIEW_PAYLOAD_HASH_MISMATCH" in result["blockers"]


def test_review_attestation_is_bound_to_the_verdicts():
    bundle = make_bundle()
    bundle["verdicts"][0]["confidence"] = 0.95
    # Re-sign the evidence envelope, but the review attestation now covers stale verdicts.
    result = run_pipeline(bundle, resign_review=False)
    assert result["status"] == "GATHER_MORE"
    assert "REVIEW_PAYLOAD_HASH_MISMATCH" in result["blockers"]


def test_review_attestation_signed_with_the_evidence_key_is_rejected():
    bundle = sign_review_attestation(make_bundle(), key=EVIDENCE_KEY, issuer=REVIEW_ISSUER)
    result = run_pipeline(bundle, resign_review=False)
    assert result["status"] == "GATHER_MORE"
    assert "REVIEW_SIGNATURE_MISMATCH" in result["blockers"]


def test_review_attestation_must_name_the_pinned_issuer():
    bundle = make_bundle()
    bundle["review_attestation"] = sign_review(
        review_payload(bundle), key=REVIEW_KEY, issuer=EVIDENCE_ISSUER, issued_at=NOW,
    )
    result = run_pipeline(bundle, resign_review=False)
    assert result["status"] == "GATHER_MORE"
    assert "REVIEW_ISSUER_MISMATCH" in result["blockers"]


def test_expired_review_attestation_fails_closed():
    bundle = sign_review_attestation(make_bundle(), now=NOW - timedelta(hours=2))
    result = run_pipeline(bundle, resign_review=False)
    assert result["status"] == "GATHER_MORE"
    assert "REVIEW_EXPIRED" in result["blockers"]


def test_review_signing_domain_must_be_distinct_from_evidence_and_operator_domains():
    cases = (
        ({"review_key": EVIDENCE_KEY}, "REVIEW_KEY_NOT_DISTINCT"),
        ({"review_key": OPERATOR_KEY}, "REVIEW_KEY_NOT_DISTINCT"),
        ({"expected_review_issuer": EVIDENCE_ISSUER}, "REVIEW_ISSUER_NOT_DISTINCT"),
        ({"expected_review_issuer": OPERATOR_ISSUER}, "REVIEW_ISSUER_NOT_DISTINCT"),
    )
    for kwargs, blocker in cases:
        result = run_pipeline(make_bundle(), **kwargs)
        assert result["status"] == "GATHER_MORE"
        assert blocker in result["blockers"], kwargs


def test_approval_signed_in_the_review_domain_is_rejected():
    bundle = _approval_bundle()
    decision = bundle["user_decision"]
    bundle["user_decision_envelope"] = sign_review(
        decision, key=REVIEW_KEY, issuer=REVIEW_ISSUER, issued_at=NOW,
    )
    result = _evaluate_with_approval(bundle)
    assert result["status"] == "DENY"
    assert "EVIDENCE_SIGNATURE_MISMATCH" in result["blockers"]
