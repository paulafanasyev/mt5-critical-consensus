from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from critical_consensus.api import app

client = TestClient(app)
TOKEN = "test-bridge-secret-that-is-long-enough"

def test_root_describes_api_and_points_to_health_without_enabling_execution():
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "critical-consensus-policy-api"
    assert body["health_path"] == "/health"
    assert body["docs_path"] == "/docs"
    assert body["execution_enabled"] is False
    assert body["live_enabled"] is False



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


def _market_snapshot(**overrides):
    now = datetime.now(timezone.utc)
    snapshot = {
        "schema_version": 1, "snapshot_id": "api-snapshot-1",
        "source_id": "untrusted-caller-label", "symbol": "EURUSD",
        "bid": 1.0840, "ask": 1.0842,
        "event_time": (now - timedelta(seconds=1)).isoformat(),
        "received_time": now.isoformat(),
    }
    snapshot.update(overrides)
    return snapshot


def test_market_snapshot_api_accepts_fresh_valid_quote_without_authorizing_trade(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", TOKEN)
    response = client.post("/v1/market/snapshot/validate", headers={"X-Bridge-Key": TOKEN},
                           json={"payload": _market_snapshot()})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "VALID_READ_ONLY_SNAPSHOT"
    assert body["valid"] is True
    assert body["trade_allowed"] is False
    assert body["blockers"] == []
    assert "order_command" not in body


def test_market_snapshot_api_rejects_schema_invalid_payload(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", TOKEN)
    response = client.post("/v1/market/snapshot/validate", headers={"X-Bridge-Key": TOKEN},
                           json={"payload": {"symbol": "EURUSD", "bid": 1.0, "ask": 1.1}})
    assert response.status_code == 200
    assert response.json()["status"] == "REJECTED"
    assert response.json()["trade_allowed"] is False
    assert "MARKET_SNAPSHOT_SCHEMA_INVALID" in response.json()["blockers"]


def test_market_snapshot_api_rejects_crossed_quotes_and_stale_event_time(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", TOKEN)
    headers = {"X-Bridge-Key": TOKEN}
    crossed = client.post("/v1/market/snapshot/validate", headers=headers,
                          json={"payload": _market_snapshot(bid=1.2, ask=1.1)}).json()
    stale = client.post("/v1/market/snapshot/validate", headers=headers,
                         json={"payload": _market_snapshot(event_time=(datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat())}).json()
    assert crossed["valid"] is False
    assert "CROSSED_QUOTE" in crossed["blockers"]
    assert stale["valid"] is False
    assert "QUOTE_STALE" in stale["blockers"]
    assert crossed["trade_allowed"] is False and stale["trade_allowed"] is False


def test_market_snapshot_api_requires_bridge_key(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", TOKEN)
    response = client.post("/v1/market/snapshot/validate", json={"payload": _market_snapshot()})
    assert response.status_code == 401
