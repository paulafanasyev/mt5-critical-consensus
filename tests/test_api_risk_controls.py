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


def test_disabling_kill_switch_fails_closed_without_a_configured_admin_token(monkeypatch):
    fake = FakeRepository()
    monkeypatch.setattr(api, "PostgresRepository", lambda: fake)
    configure(monkeypatch)
    body = {"active": False, "actor_id": "operator", "reason": "controlled test only"}
    headers = {"X-Bridge-Key": BRIDGE, "X-Kill-Switch-Admin": "any-value"}
    monkeypatch.delenv("MT5_KILL_SWITCH_ADMIN_TOKEN", raising=False)
    missing = client.post("/v1/risk/kill-switch", headers=headers, json=body)
    assert missing.status_code == 503
    assert "not configured" in missing.json()["detail"]
    monkeypatch.setenv("MT5_KILL_SWITCH_ADMIN_TOKEN", "too-short")
    short = client.post("/v1/risk/kill-switch", headers={**headers, "X-Kill-Switch-Admin": "too-short"},
                        json=body)
    assert short.status_code == 503
    assert fake.state["kill_switch_active"] is True


def test_activating_the_kill_switch_needs_no_admin_secret(monkeypatch):
    fake = FakeRepository()
    fake.state["kill_switch_active"] = False
    monkeypatch.setattr(api, "PostgresRepository", lambda: fake)
    configure(monkeypatch)
    monkeypatch.delenv("MT5_KILL_SWITCH_ADMIN_TOKEN", raising=False)
    response = client.post("/v1/risk/kill-switch", headers={"X-Bridge-Key": BRIDGE},
                           json={"active": True, "actor_id": "operator", "reason": "controlled test only"})
    assert response.status_code == 200
    assert response.json()["kill_switch_active"] is True


def test_risk_permission_route_reaches_human_review_when_evidence_agrees(monkeypatch):
    from datetime import timedelta

    from critical_consensus.risk_controls import risk_request_binding_hash

    now = datetime.now(timezone.utc)
    request = {
        "permission_id": "perm-1", "instrument": "EURUSD", "direction": "BUY", "size": 0.1,
        "mode": "demo", "expected_price": 1.1001, "sl_tp": {"sl": 1.0991, "tp": 1.1020},
        "expected_rr": 1.9, "chief_verdict": "CONFIRM", "certainty_status": "PASS",
        "compliance_status": "PASS", "risk_flags": [],
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "within_high_impact_news_window": False,
    }
    snapshot = {
        "status": "PASS", "observed_at": now.isoformat(), "account_mode": "demo",
        "symbol": "EURUSD", "symbol_group": "fx-major", "account_equity": 10000.0,
        "day_start_equity": 10000.0, "equity_high_watermark": 10200.0,
        "risk_fraction": 0.002, "risk_value_account_currency": 20.0,
        "risk_tick_size": 0.0001, "risk_tick_value_loss": 20.0,
        "daily_loss_fraction": 0.0, "drawdown_fraction": 0.0196,
        "open_positions": 1, "correlated_open_positions": 1,
        "request_binding_sha256": risk_request_binding_hash(request),
        "within_high_impact_news_window": False,
    }
    fake = FakeRepository()
    fake.state["kill_switch_active"] = False
    fake.get_latest_mt5_risk_snapshot = lambda symbol: dict(snapshot)
    monkeypatch.setattr(api, "PostgresRepository", lambda: fake)
    matrix = {"permission_matrix": {
        "instruments": ["EURUSD"], "max_size_per_trade": 0.2, "max_positions": 5,
        "max_correlated_positions": 2, "max_risk_per_trade_fraction": 0.005,
        "daily_loss_limit_fraction": 0.02, "max_drawdown_fraction": 0.10,
        "allowed_directions": ["BUY", "SELL"],
        "allowed_time_utc": {"start": "00:00", "end": "23:59"}, "rollover_block_utc": None,
        "blocked_around_news": True, "blocked_flags": ["sanctions", "mnpi"],
        "min_expected_rr": 1.5, "expected_rr_tolerance_fraction": 0.01,
        "require_stop_loss": True, "require_take_profit": True,
        "max_command_ttl_seconds": 600, "demo_enabled": True, "live_enabled": False,
    }}
    monkeypatch.setattr(api, "_yaml", lambda name: matrix)
    configure(monkeypatch)
    response = client.post("/v1/risk/permission/check", headers={"X-Bridge-Key": BRIDGE},
                           json=signed_body({"permission_request": request}))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "REQUIRE_HUMAN"
    assert body["risk_reconciliation"]["status"] == "PASS"
    assert body["trade_allowed"] is False
