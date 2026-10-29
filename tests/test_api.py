from fastapi.testclient import TestClient

from critical_consensus.api import app

client = TestClient(app)
TOKEN = "test-bridge-secret-that-is-long-enough"


def test_health_is_available_but_never_enables_execution():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["execution_enabled"] is False and body["live_enabled"] is False


def test_policy_endpoint_fails_closed_when_secret_is_not_configured(monkeypatch):
    monkeypatch.delenv("MT5_BRIDGE_TOKEN", raising=False)
    response = client.post("/v1/certainty/evaluate", json={"payload": {}})
    assert response.status_code == 503


def test_policy_endpoint_rejects_missing_or_wrong_bridge_key(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", TOKEN)
    missing = client.post("/v1/certainty/evaluate", json={"payload": {}})
    wrong = client.post("/v1/certainty/evaluate", headers={"X-Bridge-Key": "wrong"}, json={"payload": {}})
    assert missing.status_code == 401 and wrong.status_code == 401


def test_policy_endpoint_accepts_correct_bridge_key(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", TOKEN)
    response = client.post("/v1/certainty/evaluate", headers={"X-Bridge-Key": TOKEN}, json={"payload": {}})
    assert response.status_code == 200
    assert response.json()["status"] == "GATHER_MORE"
    assert response.json()["trade_allowed"] is False


def test_permission_endpoint_rejects_schema_invalid_payload(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", TOKEN)
    response = client.post("/v1/permission/check", headers={"X-Bridge-Key": TOKEN},
                           json={"payload": {"size": "0.1", "permission_id": "p"}})
    assert response.status_code == 200
    assert response.json()["status"] == "DENY"
    assert "PERMISSION_REQUEST_SCHEMA_INVALID" in response.json()["blockers"]


def test_order_submission_endpoint_stays_disabled_even_with_bridge_key(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", TOKEN)
    response = client.post("/v1/execution/submit", headers={"X-Bridge-Key": TOKEN})
    assert response.status_code == 423 and "no order was sent" in response.json()["detail"]


def test_authorization_endpoint_does_not_trust_client_supplied_permission(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", TOKEN)
    response = client.post("/v1/authorization/check", headers={"X-Bridge-Key": TOKEN}, json={"payload": {
        "permission_result": {"status": "REQUIRE_HUMAN", "permission_id": "forged"},
        "decision": {"decision": "APPROVE", "user_id": "u", "reason": "ok"}}})
    assert response.status_code == 423
    assert "server-side permission state" in response.json()["detail"]


def test_disabled_endpoints_document_locked_status_in_openapi():
    spec = client.get("/openapi.json").json()
    for path in ("/v1/authorization/check", "/v1/execution/submit"):
        responses = spec["paths"][path]["post"]["responses"]
        assert "423" in responses
        assert "200" not in responses
