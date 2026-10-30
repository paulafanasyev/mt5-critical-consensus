from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from critical_consensus.api import app
from critical_consensus.audit import canonical_hash
from critical_consensus.evidence import make_envelope
from critical_consensus.governance import sign_attestation

client = TestClient(app)

BRIDGE = "test-bridge-secret-that-is-long-enough"
EVIDENCE_KEY = "k" * 64
GOVERNANCE_KEY = "v" * 64
OPERATOR_KEY = "o" * 64
EVIDENCE_ISSUER = "test-ingress"
GOVERNANCE_ISSUER = "calibration-service"
OPERATOR_ISSUER = "operator-console"
NOW = datetime.now(timezone.utc)


def signed_body(payload, *, key=EVIDENCE_KEY, issuer=EVIDENCE_ISSUER):
    return {"payload": payload,
            "envelope": make_envelope(payload, key=key, issuer=issuer)}


def governance_attestation(*, observed_at=NOW, key=GOVERNANCE_KEY,
                           issuer=GOVERNANCE_ISSUER, domain=None):
    payload = {
        "source": "calibration-service",
        "observed_at": observed_at.isoformat(),
        "window_start": (observed_at - timedelta(days=7)).isoformat(),
        "window_end": (observed_at - timedelta(minutes=5)).isoformat(),
        "dissent_rate": 0.15, "dissent_sample_size": 100,
        "false_certainty_rate": 0.02, "false_certainty_sample_size": 100,
        "method_version": "brier-shrink-v1",
        "agent_calibration": {
            "PlatformScoutAgent": {"brier_score": 0.05, "sample_size": 100,
                                    "calibration_status": "CALIBRATED"},
        },
    }
    if domain is None:
        return sign_attestation(payload, key=key, issuer=issuer)
    return {"payload": payload,
            "envelope": make_envelope(payload, key=key, issuer=issuer, domain=domain)}


def configure(monkeypatch, *, bridge=BRIDGE, evidence_issuer=EVIDENCE_ISSUER,
              governance_issuer=GOVERNANCE_ISSUER, governance_key=GOVERNANCE_KEY):
    if bridge is None:
        monkeypatch.delenv("MT5_BRIDGE_TOKEN", raising=False)
    else:
        monkeypatch.setenv("MT5_BRIDGE_TOKEN", bridge)
    monkeypatch.setenv("MT5_EVIDENCE_HMAC_KEY", EVIDENCE_KEY)
    monkeypatch.setenv("MT5_EVIDENCE_ISSUER", evidence_issuer)
    monkeypatch.setenv("MT5_GOVERNANCE_HMAC_KEY", governance_key)
    monkeypatch.setenv("MT5_GOVERNANCE_ISSUER", governance_issuer)
    monkeypatch.setenv("MT5_OPERATOR_HMAC_KEY", OPERATOR_KEY)
    monkeypatch.setenv("MT5_OPERATOR_ISSUER", OPERATOR_ISSUER)


def test_health_is_available_but_never_enables_execution(monkeypatch):
    configure(monkeypatch)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["execution_enabled"] is False
    assert body["live_enabled"] is False
    assert body["authenticated_policy_endpoints"] is True


def test_health_reports_configured_authentication_without_secrets(monkeypatch):
    configure(monkeypatch)
    body = client.get("/health").json()
    assert body["authenticated_policy_endpoints"] is True
    assert body["bridge_token_distinct_from_signing_keys"] is True
    assert body["evidence_verification_configured"] is True
    assert body["governance_verification_configured"] is True
    assert body["operator_verification_configured"] is True
    assert body["signing_keys_distinct"] is True
    assert body["signing_issuers_distinct"] is True
    assert BRIDGE not in str(body)
    assert EVIDENCE_KEY not in str(body)
    assert GOVERNANCE_KEY not in str(body)


def test_bridge_token_that_reuses_a_signing_key_is_rejected(monkeypatch):
    configure(monkeypatch, bridge=EVIDENCE_KEY)
    response = client.post(
        "/v1/certainty/evaluate", headers={"X-Bridge-Key": EVIDENCE_KEY},
        json={**signed_body({}), "governance_attestation": governance_attestation()},
    )
    assert response.status_code == 503
    assert "MT5_EVIDENCE_HMAC_KEY" in response.json()["detail"]
    body = client.get("/health").json()
    assert body["bridge_token_distinct_from_signing_keys"] is False
    assert body["authenticated_policy_endpoints"] is False


def test_policy_endpoint_fails_closed_when_secret_is_not_configured(monkeypatch):
    configure(monkeypatch, bridge=None)
    response = client.post("/v1/certainty/evaluate",
                           json={**signed_body({}), "governance_attestation": governance_attestation()})
    assert response.status_code == 503


def test_policy_endpoint_fails_closed_without_issuer_pinning(monkeypatch):
    configure(monkeypatch, evidence_issuer="")
    response = client.post(
        "/v1/certainty/evaluate", headers={"X-Bridge-Key": BRIDGE},
        json={**signed_body({}), "governance_attestation": governance_attestation()},
    )
    assert response.status_code == 503
    assert "MT5_EVIDENCE_ISSUER" in response.json()["detail"]


def test_policy_endpoint_rejects_shared_signing_domains(monkeypatch):
    configure(monkeypatch, governance_key=EVIDENCE_KEY)
    response = client.post(
        "/v1/certainty/evaluate", headers={"X-Bridge-Key": BRIDGE},
        json={**signed_body({}), "governance_attestation": governance_attestation()},
    )
    assert response.status_code == 503
    assert "must be different" in response.json()["detail"]


def test_policy_endpoint_rejects_missing_or_wrong_bridge_key(monkeypatch):
    configure(monkeypatch)
    body = {**signed_body({}), "governance_attestation": governance_attestation()}
    missing = client.post("/v1/certainty/evaluate", json=body)
    wrong = client.post("/v1/certainty/evaluate", headers={"X-Bridge-Key": "wrong"}, json=body)
    assert missing.status_code == 401
    assert wrong.status_code == 401


def test_policy_endpoint_rejects_unsigned_or_tampered_payload(monkeypatch):
    configure(monkeypatch)
    headers = {"X-Bridge-Key": BRIDGE}
    unsigned = client.post("/v1/certainty/evaluate", headers=headers, json={"payload": {}})
    assert unsigned.status_code == 422
    tampered = dict(signed_body({"a": 1}))
    tampered["payload"] = {"a": 2}
    response = client.post("/v1/certainty/evaluate",
                           headers=headers,
                           json={**tampered, "governance_attestation": governance_attestation()})
    assert response.status_code == 403
    assert "EVIDENCE_PAYLOAD_HASH_MISMATCH" in response.json()["detail"]["blockers"]


def test_policy_endpoint_rejects_envelope_from_unexpected_issuer(monkeypatch):
    configure(monkeypatch)
    response = client.post(
        "/v1/certainty/evaluate", headers={"X-Bridge-Key": BRIDGE},
        json={**signed_body({}, issuer="rogue-ingress"),
              "governance_attestation": governance_attestation()},
    )
    assert response.status_code == 403
    assert "EVIDENCE_ISSUER_MISMATCH" in response.json()["detail"]["blockers"]


def test_policy_endpoint_rejects_invalid_or_stale_governance(monkeypatch):
    configure(monkeypatch)
    headers = {"X-Bridge-Key": BRIDGE}
    stale = auth_stale = governance_attestation(observed_at=NOW - timedelta(days=2))
    cross_domain = governance_attestation(domain="evidence")
    for attestation in (stale, cross_domain):
        response = client.post(
            "/v1/certainty/evaluate", headers=headers,
            json={**signed_body({}), "governance_attestation": attestation},
        )
        assert response.status_code == 403
        assert response.json()["detail"]["error"] == "governance_attestation_invalid"


def test_policy_endpoint_accepts_correct_bridge_key(monkeypatch):
    configure(monkeypatch)
    response = client.post("/v1/certainty/evaluate",
                           headers={"X-Bridge-Key": BRIDGE},
                           json={**signed_body({}), "governance_attestation": governance_attestation()})
    assert response.status_code == 200
    assert response.json()["status"] == "GATHER_MORE"
    assert response.json()["trade_allowed"] is False


def test_api_replaces_self_declared_calibration_with_attestation(monkeypatch):
    configure(monkeypatch)
    payload = {
        "verdicts": [{"agent_id": "PlatformScoutAgent", "verdict": "APPROVE", "confidence": 0.99,
                      "reviewer_count": 2,
                      "calibration_meta": {"brier_score": 0.9, "sample_size": 1,
                                            "calibration_status": "UNVERIFIED"}}],
    }
    response = client.post(
        "/v1/certainty/evaluate", headers={"X-Bridge-Key": BRIDGE},
        json={**signed_body(payload), "governance_attestation": governance_attestation()},
    )
    assert response.status_code == 200
    blockers = response.json()["blockers"]
    assert not any("PlatformScoutAgent:CALIBRATION" in blocker for blocker in blockers)


def _bound_metadata_payload():
    signal_input = {"signal_id": "sig-1", "instrument": "EURUSD", "timeframe": "M15"}
    digest = canonical_hash(signal_input)
    return {
        "signal_id": "sig-1", "signal_input": signal_input, "signal_input_hash": digest,
        "verdicts": [{
            "verdict_id": "v1", "signal_id": "sig-1", "agent_id": "PlatformScoutAgent",
            "verdict": "APPROVE", "confidence": 0.5, "input_hash": digest,
            "created_at": (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
            "ttl_seconds": 3600,
        }],
    }


def _evaluate_bound(payload):
    return client.post(
        "/v1/certainty/evaluate", headers={"X-Bridge-Key": BRIDGE},
        json={**signed_body(payload), "governance_attestation": governance_attestation()},
    )


def test_api_binds_every_verdict_to_the_signed_signal(monkeypatch):
    configure(monkeypatch)
    payload = _bound_metadata_payload()
    payload["verdicts"][0]["signal_id"] = "sig-other"
    response = _evaluate_bound(payload)
    assert response.status_code == 200
    body = response.json()
    assert any("VERDICT_0:VERDICT_SIGNAL_ID_MISMATCH" in blocker
               for blocker in body["blockers"])
    assert body["trade_allowed"] is False


def test_api_rejects_a_verdict_measured_against_other_inputs(monkeypatch):
    configure(monkeypatch)
    payload = _bound_metadata_payload()
    payload["verdicts"][0]["input_hash"] = "sha256:" + "a" * 64
    body = _evaluate_bound(payload).json()
    assert any("VERDICT_0:VERDICT_INPUT_HASH_DOES_NOT_MATCH_SIGNAL_INPUT" in blocker
               for blocker in body["blockers"])


def test_api_rejects_an_expired_verdict(monkeypatch):
    configure(monkeypatch)
    payload = _bound_metadata_payload()
    payload["verdicts"][0]["created_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    payload["verdicts"][0]["ttl_seconds"] = 60
    body = _evaluate_bound(payload).json()
    assert any("VERDICT_0:VERDICT_EXPIRED" in blocker for blocker in body["blockers"])


def test_order_submission_endpoint_stays_disabled_even_with_bridge_key(monkeypatch):
    configure(monkeypatch)
    response = client.post("/v1/execution/submit", headers={"X-Bridge-Key": BRIDGE})
    assert response.status_code == 423
    assert "no order was sent" in response.json()["detail"]


def test_authorization_endpoint_does_not_trust_client_supplied_permission(monkeypatch):
    configure(monkeypatch)
    response = client.post(
        "/v1/authorization/check",
        headers={"X-Bridge-Key": BRIDGE},
        json={"payload": {"permission_result": {"status": "REQUIRE_HUMAN", "permission_id": "forged"},
                         "decision": {"decision": "APPROVE", "user_id": "u", "reason": "ok"}}},
    )
    assert response.status_code == 423
    assert "server-side permission state" in response.json()["detail"]


def _review(target, alias, stance="CONFIRM", **extra):
    return {"target_verdict_id": target, "reviewer_alias": alias, "stance": stance,
            "status": "VALIDATED", "argument": "independent check",
            "evidence_refs": [f"doc-{alias}"], **extra}


def test_api_recomputes_consensus_score_instead_of_trusting_payload(monkeypatch):
    configure(monkeypatch)
    payload = {
        "verdicts": [{"verdict_id": "v1", "agent_id": "PlatformScoutAgent",
                      "verdict": "APPROVE", "confidence": 0.5}],
        "consensus_score": 0.99,
    }
    response = client.post(
        "/v1/certainty/evaluate", headers={"X-Bridge-Key": BRIDGE},
        json={**signed_body(payload), "governance_attestation": governance_attestation()},
    )
    assert response.status_code == 200
    blockers = response.json()["blockers"]
    assert any("CONSENSUS_SCORE_DOES_NOT_MATCH_VERDICTS" in blocker for blocker in blockers)
    assert response.json()["trade_allowed"] is False


def test_api_recomputes_reviewer_counts_from_review_records(monkeypatch):
    configure(monkeypatch)
    payload = {
        "verdicts": [{"verdict_id": "v1", "agent_id": "PlatformScoutAgent",
                      "verdict": "APPROVE", "confidence": 0.9, "reviewer_count": 5}],
        "peer_reviews": [_review("v1", "r1"), _review("v1", "r2")],
    }
    response = client.post(
        "/v1/certainty/evaluate", headers={"X-Bridge-Key": BRIDGE},
        json={**signed_body(payload), "governance_attestation": governance_attestation()},
    )
    assert response.status_code == 200
    blockers = response.json()["blockers"]
    assert any("REVIEWER_COUNT_DOES_NOT_MATCH_RECORDS:v1" in blocker for blocker in blockers)


def test_api_rejects_open_challenge_hidden_from_ledger(monkeypatch):
    configure(monkeypatch)
    payload = {
        "verdicts": [{"verdict_id": "v1", "agent_id": "PlatformScoutAgent",
                      "verdict": "APPROVE", "confidence": 0.9}],
        "peer_reviews": [_review("v1", "r1"),
                         _review("v1", "r2", stance="CHALLENGE", challenge_type="EVIDENCE_GAP")],
        "open_challenges": [],
        "challenge_records": [{"challenge_id": "c1", "target_verdict_id": "v1",
                               "argument": "source is stale", "status": "OPEN",
                               "rounds": [], "evidence_refs": ["doc-c1"]}],
    }
    response = client.post(
        "/v1/certainty/evaluate", headers={"X-Bridge-Key": BRIDGE},
        json={**signed_body(payload), "governance_attestation": governance_attestation()},
    )
    assert response.status_code == 200
    blockers = response.json()["blockers"]
    assert any("OPEN_CHALLENGE_STATE_CONTRADICTS_LEDGER" in blocker for blocker in blockers)


def test_permission_route_refuses_caller_supplied_risk_aggregates(monkeypatch):
    configure(monkeypatch)
    response = client.post("/v1/permission/check", headers={"X-Bridge-Key": BRIDGE},
                           json={"payload": {}})
    assert response.status_code == 423
    assert "/v1/risk/permission/check" in response.json()["detail"]


def test_policy_endpoint_rejects_shared_signing_issuers(monkeypatch):
    configure(monkeypatch, governance_issuer=EVIDENCE_ISSUER)
    response = client.post(
        "/v1/certainty/evaluate", headers={"X-Bridge-Key": BRIDGE},
        json={**signed_body({}), "governance_attestation": governance_attestation()},
    )
    assert response.status_code == 503
    assert "issuers" in response.json()["detail"]


def test_health_is_hermetic_when_no_secrets_are_configured(monkeypatch):
    for name in ("MT5_BRIDGE_TOKEN", "MT5_EVIDENCE_HMAC_KEY", "MT5_GOVERNANCE_HMAC_KEY",
                 "MT5_OPERATOR_HMAC_KEY", "MT5_EVIDENCE_ISSUER", "MT5_GOVERNANCE_ISSUER",
                 "MT5_OPERATOR_ISSUER", "MT5_KILL_SWITCH_ADMIN_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    body = client.get("/health").json()
    assert body["execution_enabled"] is False
    assert body["authenticated_policy_endpoints"] is False
    assert body["evidence_verification_configured"] is False
    assert body["governance_verification_configured"] is False
    assert body["operator_verification_configured"] is False


def test_execution_endpoints_can_never_return_or_accept_an_order(monkeypatch):
    configure(monkeypatch)
    headers = {"X-Bridge-Key": BRIDGE}
    fetched = client.get("/v1/execution/next", headers=headers)
    assert fetched.status_code == 204
    assert fetched.content == b""
    submitted = client.post("/v1/execution/submit", headers=headers, json={"payload": {}})
    assert submitted.status_code == 423
    assert "no order was sent" in submitted.json()["detail"]


def test_execution_endpoints_require_bridge_auth(monkeypatch):
    configure(monkeypatch)
    assert client.get("/v1/execution/next").status_code == 401
    assert client.post("/v1/execution/submit", json={}).status_code == 401
