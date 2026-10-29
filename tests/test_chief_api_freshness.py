from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from critical_consensus.api import app

client = TestClient(app)
TOKEN = "test-bridge-secret-that-is-long-enough"


def test_direct_chief_api_rejects_stale_reasoning_before_gate(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", TOKEN)
    stale = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
    response = client.post(
        "/v1/chief/review",
        headers={"X-Bridge-Key": TOKEN},
        json={"payload": {"created_at": stale, "reasoning_hash": "sha256:" + "0" * 64}},
    )
    assert response.status_code == 200
    assert response.json()["verdict"] == "HOLD"
    assert response.json()["blockers"] == ["CHIEF_REASONING_EXPIRED"]


def test_direct_chief_api_rejects_missing_or_mismatched_hash(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", TOKEN)
    fresh = datetime.now(timezone.utc).isoformat()
    for payload, blocker in (
        ({"created_at": fresh}, "CHIEF_REASONING_HASH_MISSING_OR_INVALID"),
        ({"created_at": fresh, "reasoning_hash": "sha256:" + "0" * 64}, "CHIEF_REASONING_HASH_MISMATCH"),
    ):
        response = client.post("/v1/chief/review", headers={"X-Bridge-Key": TOKEN}, json={"payload": payload})
        assert response.status_code == 200
        assert response.json()["verdict"] == "HOLD"
        assert blocker in response.json()["blockers"]
