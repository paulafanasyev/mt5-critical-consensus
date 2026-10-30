from datetime import datetime, timezone

from fastapi.testclient import TestClient

from critical_consensus import api
from critical_consensus.evidence import make_envelope


client = TestClient(api.app)
BRIDGE = "a-valid-bridge-token-longer-than-24"
EVIDENCE_KEY = "k" * 64


def signed_body(payload):
    return {"payload": payload,
            "envelope": make_envelope(payload, key=EVIDENCE_KEY, issuer="test-ingress")}


def configure(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", BRIDGE)
    monkeypatch.setenv("MT5_EVIDENCE_HMAC_KEY", EVIDENCE_KEY)
    monkeypatch.setenv("MT5_EVIDENCE_ISSUER", "test-ingress")


class FakeRepository:
    def __init__(self):
        self.state = {"kill_switch_active": True, "status": "PERSISTED",
                      "updated_at": datetime.now(timezone.utc).isoformat(),
                      "updated_by": "bootstrap", "reason": "active by default"}

    def get_kill_switch_state(self):
        return dict(self.state)

    def set_kill_switch(self, *, active, actor_id, reason):
        self.state = {"kill_switch_active": active, "status": "PERSISTED",
                      "updated_at": datetime.now(timezone.utc).isoformat(),
                      "updated_by": actor_id, "reason": reason}
        return dict(self.state)

    def get_latest_mt5_risk_snapshot(self, symbol):
        return None


def test_global_kill_switch_default_is_visible_and_persisted(monkeypatch):
    fake = FakeRepository()
    monkeypatch.setattr(api, "PostgresRepository", lambda: fake)
    configure(monkeypatch)
    response = client.get("/v1/risk/kill-switch", headers={"X-Bridge-Key": BRIDGE})
    assert response.status_code == 200
    assert response.json()["kill_switch_active"] is True


def test_disabling_kill_switch_requires_separate_admin_secret(monkeypatch):
    fake = FakeRepository()
    monkeypatch.setattr(api, "PostgresRepository", lambda: fake)
    configure(monkeypatch)
    monkeypatch.setenv("MT5_KILL_SWITCH_ADMIN_TOKEN", "a-different-admin-token-at-least-32-characters")
    bridge = {"X-Bridge-Key": BRIDGE}
    body = {"active": False, "actor_id": "operator", "reason": "controlled test only"}

    denied = client.post("/v1/risk/kill-switch", headers=bridge, json=body)
    assert denied.status_code == 403
    wrong = client.post("/v1/risk/kill-switch", headers={**bridge, "X-Kill-Switch-Admin": "wrong"},
                        json=body)
    assert wrong.status_code == 403
    accepted = client.post("/v1/risk/kill-switch",
                           headers={**bridge, "X-Kill-Switch-Admin": "a-different-admin-token-at-least-32-characters"},
                           json=body)
    assert accepted.status_code == 200
    assert accepted.json()["kill_switch_active"] is False


def test_risk_permission_route_denies_without_real_persisted_terminal_snapshot(monkeypatch):
    fake = FakeRepository()
    fake.state["kill_switch_active"] = False
    monkeypatch.setattr(api, "PostgresRepository", lambda: fake)
    configure(monkeypatch)
    response = client.post(
        "/v1/risk/permission/check", headers={"X-Bridge-Key": BRIDGE},
        json=signed_body({"instrument": "EURUSD", "mode": "demo"}),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "DENY"
    assert "RISK_SNAPSHOT_MISSING" in response.json()["blockers"]
    assert response.json()["trade_allowed"] is False
