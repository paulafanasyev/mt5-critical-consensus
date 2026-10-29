from fastapi.testclient import TestClient

from critical_consensus.api import app

client = TestClient(app)


def test_health_is_available_but_never_enables_execution():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["execution_enabled"] is False
    assert body["live_enabled"] is False


def test_policy_endpoint_fails_closed_when_secret_is_not_configured(monkeypatch):
    monkeypatch.delenv("MT5_BRIDGE_TOKEN", raising=False)
    response = client.post("/v1/certainty/evaluate", json={"payload": {}})
    assert response.status_code == 503


def test_policy_endpoint_rejects_missing_or_wrong_bridge_key(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", "test-bridge-secret-that-is-long-enough")
    missing = client.post("/v1/certainty/evaluate", json={"payload": {}})
    wrong = client.post("/v1/certainty/evaluate", headers={"X-Bridge-Key": "wrong"}, json={"payload": {}})
    assert missing.status_code == 401
    assert wrong.status_code == 401


def test_policy_endpoint_accepts_correct_bridge_key(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", "test-bridge-secret-that-is-long-enough")
    response = client.post("/v1/certainty/evaluate",
                           headers={"X-Bridge-Key": "test-bridge-secret-that-is-long-enough"},
                           json={"payload": {}})
    assert response.status_code == 200
    assert response.json()["status"] == "GATHER_MORE"
    assert response.json()["trade_allowed"] is False


def test_order_submission_endpoint_stays_disabled_even_with_bridge_key(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", "test-bridge-secret-that-is-long-enough")
    response = client.post("/v1/execution/submit", headers={"X-Bridge-Key": "test-bridge-secret-that-is-long-enough"})
    assert response.status_code == 423
    assert "no order was sent" in response.json()["detail"]


def test_authorization_endpoint_does_not_trust_client_supplied_permission(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", "test-bridge-secret-that-is-long-enough")
    response = client.post(
        "/v1/authorization/check",
        headers={"X-Bridge-Key": "test-bridge-secret-that-is-long-enough"},
        json={"payload": {"permission_result": {"status": "REQUIRE_HUMAN", "permission_id": "forged"},
                         "decision": {"decision": "APPROVE", "user_id": "u", "reason": "ok"}}},
    )
    assert response.status_code == 423
    assert "server-side permission state" in response.json()["detail"]
