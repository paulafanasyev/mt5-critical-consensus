from datetime import datetime, timezone, timedelta
from critical_consensus.gates import certainty_gate, chief_skeptic_pass, permission_gate, user_authorization_gate
from critical_consensus.audit import canonical_hash

POLICY = {
    "near_certainty_threshold": 0.98, "consensus_score_threshold": 0.98,
    "min_reviewers_per_verdict": 2, "brier_threshold": 0.12,
    "source_credibility_threshold": 0.75, "source_max_age_seconds": 900,
    "min_calibration_sample_size": 30,
    "dissent_rate_floor": 0.10, "min_dissent_samples": 30,
    "false_certainty_rate_max": 0.05, "min_false_certainty_samples": 30,
}

def good_payload():
    agents = ["PlatformScoutAgent", "TechnicalAnalystAgent", "RiskManagerAgent", "ComplianceAgent"]
    return {
        "mandatory_agents": agents,
        "verdicts": [{"agent_id": a, "verdict": "APPROVE", "confidence": 0.99,
                      "reviewer_count": 2, "calibration_meta": {"brier_score": 0.08, "sample_size": 100, "calibration_status": "CALIBRATED"}}
                     for a in agents],
        "consensus_score": 0.99, "open_challenges": [], "challenge_resolution_status": "RESOLVED", "contradictions_last_n_hours": 0,
        "devil_advocate_report": {"status": "PASS", "counter_hypotheses": [
            {"hypothesis": "h1", "evidence_refs": ["e1"]},
            {"hypothesis": "h2", "evidence_refs": ["e2"]},
            {"hypothesis": "h3", "evidence_refs": ["e3"]}]},
        "key_sources": [{"source_id": "s1", "credibility": 0.90, "age_seconds": 20, "ttl_seconds": 900},
                        {"source_id": "s2", "credibility": 0.88, "age_seconds": 50, "ttl_seconds": 900}],
        "risk_flags": [], "dissent_rate": 0.15, "dissent_sample_size": 100,
        "false_certainty_rate": 0.02, "false_certainty_sample_size": 100,
    }

def test_certainty_pass_is_not_trade_authorization():
    result = certainty_gate(good_payload(), POLICY)
    assert result["status"] == "PASS"
    assert result["trade_allowed"] is False

def test_single_reject_blocks_certainty_gate():
    payload = good_payload()
    payload["verdicts"][0]["verdict"] = "REJECT"
    result = certainty_gate(payload, POLICY)
    assert result["status"] == "GATHER_MORE"
    assert result["trade_allowed"] is False

def test_open_devil_challenge_blocks_gate():
    payload = good_payload()
    payload["open_challenges"] = [{"challenge_id": "da-1", "status": "OPEN", "source": "DevilAdvocateAgent"}]
    payload["challenge_resolution_status"] = "OPEN"
    result = certainty_gate(payload, POLICY)
    assert result["status"] == "GATHER_MORE"
    assert not result["passed"]

def test_deadlock_escalates_without_trade():
    payload = good_payload()
    payload["challenge_resolution_status"] = "DEADLOCK"
    result = certainty_gate(payload, POLICY)
    assert result["status"] == "ESCALATE"
    assert result["trade_allowed"] is False

def test_abstain_without_reason_blocks():
    payload = good_payload()
    payload["verdicts"][0]["verdict"] = "ABSTAIN"
    payload["verdicts"][0]["abstain_reason"] = ""
    assert certainty_gate(payload, POLICY)["status"] == "GATHER_MORE"

def test_missing_chief_step_cannot_confirm():
    steps = {k: {"passed": True, "detail": "checked", "evidence_refs": [f"ev-{k}"]} for k in "abcdefgh"}
    result = chief_skeptic_pass({"verdict": "CONFIRM", "steps": steps, "reasons_against": ["a", "b", "c", "d", "e"], "tail_risk_probability": 0.01, "open_challenges": [], "reasoning_hash": "sha256:0123456789abcdef"})
    assert result["verdict"] == "HOLD"

def test_chief_confirm_requires_all_a_to_i():
    reasoning = {"reasoning_id": "chief-r-1", "signal_id": "sig-1", "created_at": datetime.now(timezone.utc).isoformat(),
                 "verdict": "CONFIRM", "steps": {k: {"passed": True, "detail": "evidence checked", "evidence_refs": [f"ev-{k}"]} for k in "abcdefghi"},
                 "reasons_against": ["macro", "news", "correlation", "liquidity", "regime"], "tail_risk_probability": 0.01,
                 "tail_risk_scenarios": [{"scenario": "central bank shock", "probability": 0.01, "impact": "repricing", "evidence_refs": ["risk-1"]}],
                 "open_challenges": [], "all_challenges_read": True, "challenge_ledger_snapshot": [], "challenge_ledger_hash": canonical_hash([]), "reviewed_challenge_ids": []}
    reasoning["reasoning_hash"] = canonical_hash(reasoning)
    result = chief_skeptic_pass(reasoning)
    assert result["verdict"] == "CONFIRM"
    assert result["trade_allowed"] is False

def valid_permission_request(now):
    return {"permission_id": "perm-1", "instrument": "EURUSD", "direction": "BUY", "size": 0.1,
            "mode": "demo", "chief_verdict": "CONFIRM", "certainty_status": "PASS", "compliance_status": "PASS",
            "risk_flags": [], "expected_rr": 2.5, "expected_price": 1.0840,
            "sl_tp": {"sl": 1.0820, "tp": 1.0890}, "expires_at": (now + timedelta(minutes=5)).isoformat(),
            "within_high_impact_news_window": False,
            "risk_snapshot": {"risk_fraction": 0.003, "daily_loss_fraction": 0.004,
                              "drawdown_fraction": 0.02, "open_positions": 1,
                              "correlated_open_positions": 0}}

def test_permission_gate_requires_human():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    matrix = {"permission_matrix": {"instruments": ["EURUSD"], "max_size_per_trade": 0.2,
             "allowed_directions": ["BUY", "SELL"], "demo_enabled": True, "live_enabled": False,
             "blocked_flags": ["MNPI"], "min_expected_rr": 1.5, "require_stop_loss": True,
             "require_take_profit": True, "max_command_ttl_seconds": 600,
             "allowed_time_utc": {"start": "07:00", "end": "19:00"},
             "rollover_block_utc": {"start": "20:55", "end": "21:10"},
             "blocked_around_news": True, "max_risk_per_trade_fraction": 0.005,
             "daily_loss_limit_fraction": 0.02, "max_drawdown_fraction": 0.10,
             "max_positions": 5, "max_correlated_positions": 2}}
    result = permission_gate(valid_permission_request(now), matrix, now=now)
    assert result["status"] == "REQUIRE_HUMAN"
    assert result["allowed"] is False

def test_live_never_passes_when_live_disabled():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    matrix = {"permission_matrix": {"instruments": ["EURUSD"], "max_size_per_trade": 0.2,
             "allowed_directions": ["BUY", "SELL"], "demo_enabled": True, "live_enabled": False,
             "blocked_flags": [], "min_expected_rr": 1.5, "require_stop_loss": True,
             "require_take_profit": True, "max_command_ttl_seconds": 600,
             "allowed_time_utc": {"start": "07:00", "end": "19:00"},
             "rollover_block_utc": {"start": "20:55", "end": "21:10"},
             "blocked_around_news": True, "max_risk_per_trade_fraction": 0.005,
             "daily_loss_limit_fraction": 0.02, "max_drawdown_fraction": 0.10,
             "max_positions": 5, "max_correlated_positions": 2}}
    req = valid_permission_request(now); req["mode"] = "live"
    assert "LIVE_MODE_DISABLED" in permission_gate(req, matrix, now=now)["blockers"]

def test_live_authorization_requires_2fa_and_signature():
    now = datetime.now(timezone.utc)
    perm = {"status": "REQUIRE_HUMAN", "permission_id": "p1", "mode": "live", "expires_at": (now + timedelta(minutes=5)).isoformat()}
    decision = {"decision": "APPROVE", "user_id": "operator-1", "reason": "reviewed risks",
                "permission_id": "p1", "timestamp": now.isoformat(), "ttl_seconds": 600,
                "two_fa_verified": False, "signature_verified": False}
    result = user_authorization_gate(perm, decision, now=now)
    assert result["status"] == "DENY"
    assert "LIVE_2FA_REQUIRED" in result["blockers"]
    assert "LIVE_SIGNATURE_REQUIRED" in result["blockers"]

def test_expired_authorization_is_denied():
    now = datetime.now(timezone.utc)
    perm = {"status": "REQUIRE_HUMAN", "permission_id": "p1", "mode": "demo", "expires_at": (now + timedelta(minutes=5)).isoformat()}
    decision = {"decision": "APPROVE", "user_id": "u1", "reason": "reviewed",
                "permission_id": "p1", "timestamp": (now - timedelta(minutes=20)).isoformat(), "ttl_seconds": 600}
    result = user_authorization_gate(perm, decision, now=now)
    assert result["status"] == "DENY"
    assert "DECISION_EXPIRED" in result["blockers"]


def test_permission_gate_blocks_news_window():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    matrix = {"permission_matrix": {"instruments": ["EURUSD"], "max_size_per_trade": 0.2,
             "allowed_directions": ["BUY", "SELL"], "demo_enabled": True, "live_enabled": False,
             "blocked_flags": [], "min_expected_rr": 1.5, "require_stop_loss": True,
             "require_take_profit": True, "max_command_ttl_seconds": 600,
             "allowed_time_utc": {"start": "07:00", "end": "19:00"},
             "rollover_block_utc": {"start": "20:55", "end": "21:10"},
             "blocked_around_news": True, "max_risk_per_trade_fraction": 0.005,
             "daily_loss_limit_fraction": 0.02, "max_drawdown_fraction": 0.10,
             "max_positions": 5, "max_correlated_positions": 2}}
    req = valid_permission_request(now); req["within_high_impact_news_window"] = True
    assert "NEWS_WINDOW_ACTIVE_OR_UNKNOWN" in permission_gate(req, matrix, now=now)["blockers"]

def test_permission_gate_blocks_daily_loss_limit():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    matrix = {"permission_matrix": {"instruments": ["EURUSD"], "max_size_per_trade": 0.2,
             "allowed_directions": ["BUY", "SELL"], "demo_enabled": True, "live_enabled": False,
             "blocked_flags": [], "min_expected_rr": 1.5, "require_stop_loss": True,
             "require_take_profit": True, "max_command_ttl_seconds": 600,
             "allowed_time_utc": {"start": "07:00", "end": "19:00"},
             "blocked_around_news": True, "max_risk_per_trade_fraction": 0.005,
             "daily_loss_limit_fraction": 0.02, "max_drawdown_fraction": 0.10,
             "max_positions": 5, "max_correlated_positions": 2}}
    req = valid_permission_request(now); req["risk_snapshot"]["daily_loss_fraction"] = 0.02
    assert "DAILY_LOSS_LIMIT_REACHED" in permission_gate(req, matrix, now=now)["blockers"]

def test_permission_gate_fails_closed_when_risk_snapshot_missing():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    matrix = {"permission_matrix": {"instruments": ["EURUSD"], "max_size_per_trade": 0.2,
             "allowed_directions": ["BUY", "SELL"], "demo_enabled": True, "live_enabled": False,
             "blocked_flags": [], "min_expected_rr": 1.5, "require_stop_loss": True,
             "require_take_profit": True, "max_command_ttl_seconds": 600,
             "allowed_time_utc": {"start": "07:00", "end": "19:00"},
             "blocked_around_news": True, "max_risk_per_trade_fraction": 0.005,
             "daily_loss_limit_fraction": 0.02, "max_drawdown_fraction": 0.10,
             "max_positions": 5, "max_correlated_positions": 2}}
    req = valid_permission_request(now); req.pop("risk_snapshot")
    assert "RISK_SNAPSHOT_MISSING" in permission_gate(req, matrix, now=now)["blockers"]


def test_valid_demo_approval_is_one_time_allow_not_order_execution():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    permission = {"status": "REQUIRE_HUMAN", "permission_id": "p2", "mode": "demo", "expires_at": (now + timedelta(minutes=5)).isoformat()}
    decision = {"decision": "APPROVE", "user_id": "operator-1", "reason": "reviewed dissent and tail risk",
                "permission_id": "p2", "timestamp": now.isoformat(), "ttl_seconds": 300}
    result = user_authorization_gate(permission, decision, now=now)
    assert result["status"] == "ALLOW_ONCE"
    assert result["authorized"] is True
    assert result["trade_allowed"] is False


def test_policy_owned_mandatory_agents_cannot_be_overridden_by_payload():
    payload = good_payload()
    payload["mandatory_agents"] = ["PlatformScoutAgent"]
    policy = dict(POLICY, mandatory_agents=["PlatformScoutAgent", "ComplianceAgent", "ChiefAgent"] )
    result = certainty_gate(payload, policy)
    assert result["status"] == "GATHER_MORE"
    assert "MISSING_MANDATORY_AGENTS:ChiefAgent" in result["blockers"]


def test_certainty_gate_blocks_groupthink_and_high_false_certainty():
    payload = good_payload()
    payload["dissent_rate"] = 0.02
    payload["false_certainty_rate"] = 0.09
    result = certainty_gate(payload, POLICY)
    assert result["status"] == "GATHER_MORE"
    assert "GROUPTHINK_ALARM_OR_DISSENT_METRIC_MISSING" in result["blockers"]
    assert "FALSE_CERTAINTY_RATE_HIGH_OR_METRIC_UNVERIFIED" in result["blockers"]


def test_certainty_gate_requires_verified_health_metric_sample_sizes():
    payload = good_payload()
    payload["dissent_sample_size"] = 1
    payload["false_certainty_sample_size"] = 1
    result = certainty_gate(payload, POLICY)
    assert result["status"] == "GATHER_MORE"
    assert "GROUPTHINK_ALARM_OR_DISSENT_METRIC_MISSING" in result["blockers"]
    assert "FALSE_CERTAINTY_RATE_HIGH_OR_METRIC_UNVERIFIED" in result["blockers"]


def test_chief_cannot_confirm_when_open_challenge_is_hidden_in_ledger():
    ledger = [{"challenge_id": "ch-1", "status": "OPEN"}]
    reasoning = {"reasoning_id": "chief-r-2", "signal_id": "sig-2", "created_at": datetime.now(timezone.utc).isoformat(),
                 "verdict": "CONFIRM", "steps": {k: {"passed": True, "detail": "evidence checked", "evidence_refs": [f"ev-{k}"]} for k in "abcdefghi"},
                 "reasons_against": ["macro", "news", "correlation", "liquidity", "regime"], "tail_risk_probability": 0.01,
                 "tail_risk_scenarios": [{"scenario": "central bank shock", "probability": 0.01, "impact": "repricing", "evidence_refs": ["risk-1"]}],
                 "open_challenges": [], "all_challenges_read": True, "challenge_ledger_snapshot": ledger,
                 "challenge_ledger_hash": canonical_hash(ledger), "reviewed_challenge_ids": ["ch-1"]}
    reasoning["reasoning_hash"] = canonical_hash(reasoning)
    result = chief_skeptic_pass(reasoning)
    assert result["verdict"] == "HOLD"
    assert "CHIEF_CHALLENGE_LEDGER_CONTAINS_UNRESOLVED_CHALLENGE" in result["blockers"]


def test_certainty_gate_rejects_nan_confidence_consensus_and_calibration():
    payload = good_payload()
    payload["verdicts"][0]["confidence"] = float("nan")
    payload["verdicts"][1]["calibration_meta"]["brier_score"] = float("nan")
    payload["consensus_score"] = float("nan")
    result = certainty_gate(payload, POLICY)
    assert result["status"] == "GATHER_MORE"
    assert "PlatformScoutAgent:INVALID_CONFIDENCE" in result["blockers"]
    assert "TechnicalAnalystAgent:CALIBRATION_MISSING_UNVERIFIED_OR_OUT_OF_LIMIT" in result["blockers"]
    assert "CONSENSUS_SCORE_MISSING_INVALID_OR_BELOW_THRESHOLD" in result["blockers"]


def test_permission_gate_rejects_nan_size_and_fabricated_reward_risk():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    matrix = {"permission_matrix": {"instruments": ["EURUSD"], "max_size_per_trade": 0.2,
             "allowed_directions": ["BUY", "SELL"], "demo_enabled": True, "live_enabled": False,
             "blocked_flags": [], "min_expected_rr": 1.5, "expected_rr_tolerance_fraction": 0.1,
             "require_stop_loss": True, "require_take_profit": True, "max_command_ttl_seconds": 600,
             "allowed_time_utc": {"start": "07:00", "end": "19:00"},
             "blocked_around_news": True, "max_risk_per_trade_fraction": 0.005,
             "daily_loss_limit_fraction": 0.02, "max_drawdown_fraction": 0.10,
             "max_positions": 5, "max_correlated_positions": 2}}
    req = valid_permission_request(now)
    req["size"] = float("nan")
    req["expected_rr"] = 99.0
    result = permission_gate(req, matrix, now=now)
    assert result["status"] == "DENY"
    assert "SIZE_INVALID_OR_OUT_OF_POLICY" in result["blockers"]
    assert "EXPECTED_RR_DOES_NOT_MATCH_SL_TP" in result["blockers"]


def test_certainty_gate_malformed_top_level_payload_fails_closed():
    result = certainty_gate(None, POLICY)
    assert result["status"] == "GATHER_MORE"
    assert result["trade_allowed"] is False
    assert "INVALID_CERTAINTY_PAYLOAD" in result["blockers"]


def test_permission_gate_malformed_top_level_request_fails_closed():
    result = permission_gate(None, {"permission_matrix": {}})
    assert result["status"] == "DENY"
    assert result["trade_allowed"] is False
    assert "INVALID_PERMISSION_REQUEST" in result["blockers"]


def test_self_adversarial_pass_does_not_approve_low_confidence():
    from critical_consensus.gates import self_adversarial_pass
    sources = [{"source_id": "s1", "credibility": 0.9, "min_credibility": 0.75, "age_seconds": 5, "ttl_seconds": 60}]
    abstain = self_adversarial_pass({"verdict": "APPROVE", "confidence": 0.72, "verdict_id": "v1"},
                                    counterarguments=["x", "y", "z"], contradictions=[], sources=sources)
    assert abstain["verdict"] == "ABSTAIN"
    assert "CONFIDENCE_BELOW_APPROVAL_FLOOR" in abstain["blockers"]
    reject = self_adversarial_pass({"verdict": "APPROVE", "confidence": 0.2, "verdict_id": "v2"},
                                   counterarguments=["x", "y", "z"], contradictions=[], sources=sources)
    assert reject["verdict"] == "REJECT"
    assert "CONFIDENCE_BELOW_REJECTION_FLOOR" in reject["blockers"]


def test_chief_gate_rejects_coerced_nested_evidence_and_reasons():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    def valid_reasoning():
        reasoning = {"reasoning_id": "chief-r-strict", "signal_id": "sig-strict", "created_at": now.isoformat(),
            "verdict": "CONFIRM",
            "steps": {k: {"passed": True, "detail": "evidence checked", "evidence_refs": [f"ev-{k}"]} for k in "abcdefghi"},
            "reasons_against": ["macro", "news", "correlation", "liquidity", "regime"],
            "tail_risk_probability": 0.01,
            "tail_risk_scenarios": [{"scenario": "shock", "probability": 0.01, "impact": "repricing", "evidence_refs": ["risk-1"]}],
            "open_challenges": [], "all_challenges_read": True, "challenge_ledger_snapshot": [],
            "challenge_ledger_hash": canonical_hash([]), "reviewed_challenge_ids": []}
        reasoning["reasoning_hash"] = canonical_hash(reasoning)
        return reasoning
    mutations = []
    bad = valid_reasoning(); bad["steps"]["a"]["detail"] = 42; mutations.append(bad)
    bad = valid_reasoning(); bad["steps"]["a"]["evidence_refs"] = [42]; mutations.append(bad)
    bad = valid_reasoning(); bad["reasons_against"] = ["macro", "news", "correlation", "liquidity", 5]; mutations.append(bad)
    bad = valid_reasoning(); bad["tail_risk_scenarios"][0]["scenario"] = 123; mutations.append(bad)
    for reasoning in mutations:
        reasoning["reasoning_hash"] = canonical_hash({k: v for k, v in reasoning.items() if k != "reasoning_hash"})
        result = chief_skeptic_pass(reasoning, now=now)
        assert result["verdict"] == "HOLD"
        assert result["trade_allowed"] is False


def test_user_authorization_requires_id_and_bounded_integer_ttl():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    permission = {"status": "REQUIRE_HUMAN", "permission_id": "p3", "mode": "demo",
                  "expires_at": (now + timedelta(minutes=5)).isoformat()}
    base = {"decision_id": "decision-3", "decision": "APPROVE", "user_id": "operator-1",
            "reason": "reviewed", "permission_id": "p3", "timestamp": now.isoformat(), "ttl_seconds": 300}
    for mutation in ("missing_id", "over_max", "float_ttl", "lowercase_decision"):
        decision = dict(base)
        if mutation == "missing_id": decision.pop("decision_id")
        elif mutation == "over_max": decision["ttl_seconds"] = 601
        elif mutation == "float_ttl": decision["ttl_seconds"] = 300.0
        else: decision["decision"] = "approve"
        result = user_authorization_gate(permission, decision, now=now)
        assert result["status"] == "DENY"
        assert result["authorized"] is False


def test_certainty_gate_rejects_duplicate_source_ids():
    payload = good_payload()
    payload["key_sources"].append(dict(payload["key_sources"][0]))
    result = certainty_gate(payload, POLICY)
    assert result["status"] == "GATHER_MORE"
    assert "KEY_SOURCE_IDS_INVALID_OR_DUPLICATE" in result["blockers"]
