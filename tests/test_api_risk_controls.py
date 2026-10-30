from datetime import datetime, timezone

from fastapi.testclient import TestClient

from critical_consensus import api
from critical_consensus.evidence import make_envelope, sign_bundle
from critical_consensus.risk_controls import risk_request_binding_hash
from test_pipeline import (
    CERTAINTY_POLICY, EVIDENCE_ISSUER, EVIDENCE_KEY, GOVERNANCE_ISSUER,
    GOVERNANCE_KEY, OPERATOR_ISSUER, OPERATOR_KEY, PERMISSION_MATRIX, REVIEW_ISSUER,
    REVIEW_KEY, make_bundle, make_governance_attestation, sign_review_attestation,
)


client = TestClient(api.app)
BRIDGE = "a-valid-bridge-token-longer-than-24"


def signed_body(payload):
    return {"payload": payload,
            "envelope": make_envelope(payload, key=EVIDENCE_KEY, issuer=EVIDENCE_ISSUER)}


def configure(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", BRIDGE)
    monkeypatch.setenv("MT5_EVIDENCE_HMAC_KEY", EVIDENCE_KEY)
    monkeypatch.setenv("MT5_EVIDENCE_ISSUER", EVIDENCE_ISSUER)
    monkeypatch.setenv("MT5_GOVERNANCE_HMAC_KEY", GOVERNANCE_KEY)
    monkeypatch.setenv("MT5_GOVERNANCE_ISSUER", GOVERNANCE_ISSUER)
    monkeypatch.setenv("MT5_OPERATOR_HMAC_KEY", OPERATOR_KEY)
    monkeypatch.setenv("MT5_OPERATOR_ISSUER", OPERATOR_ISSUER)
    monkeypatch.setenv("MT5_REVIEW_HMAC_KEY", REVIEW_KEY)
    monkeypatch.setenv("MT5_REVIEW_ISSUER", REVIEW_ISSUER)


def sign_signal_bundle(bundle, now):
    body = sign_review_attestation(dict(bundle), now=now)
    body["governance_attestation"] = make_governance_attestation(now)
    return sign_bundle(body, key=EVIDENCE_KEY, issuer=EVIDENCE_ISSUER, issued_at=now)


def risk_snapshot_for(bundle, now):
    request = bundle["permission_request"]
    return {
        "status": "PASS", "observed_at": now.isoformat(), "account_mode": "demo",
        "symbol": request["instrument"], "symbol_group": "fx-major",
        "account_equity": 10000.0, "account_balance": 10000.0, "daily_pnl": 0.0,
        "day_start_equity": 10000.0, "equity_high_watermark": 10200.0,
        "risk_fraction": 0.004, "risk_value_account_currency": 40.0,
        "risk_tick_size": 0.0001, "risk_tick_value_loss": 20.0,
        "reference_price": request["expected_price"],
        "daily_loss_fraction": 0.0, "drawdown_fraction": 0.0196,
        "open_positions": 1, "correlated_open_positions": 0, "open_stop_risk_fraction": 0.0,
        "request_binding_sha256": risk_request_binding_hash(request),
        "within_high_impact_news_window": False,
    }


def _policy(name):
    if name == "certainty_thresholds.yaml":
        return dict(CERTAINTY_POLICY)
    matrix = {**PERMISSION_MATRIX, "permission_matrix": dict(PERMISSION_MATRIX["permission_matrix"])}
    matrix["permission_matrix"]["allowed_time_utc"] = {"start": "00:00", "end": "23:59"}
    matrix["permission_matrix"]["rollover_block_utc"] = None
    return matrix


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


def test_risk_permission_route_refuses_caller_supplied_risk_aggregates(monkeypatch):
    configure(monkeypatch)
    response = client.post("/v1/permission/check", headers={"X-Bridge-Key": BRIDGE},
                           json={"payload": {}})
    assert response.status_code == 423


class RaisingRepository:
    def get_kill_switch_state(self):
        raise RuntimeError("database unavailable")

    def set_kill_switch(self, **kwargs):
        raise RuntimeError("database unavailable")

    def get_latest_mt5_risk_snapshot(self, symbol):
        raise RuntimeError("database unavailable")


def test_risk_endpoints_fail_closed_when_the_database_is_unavailable(monkeypatch):
    monkeypatch.setattr(api, "PostgresRepository", lambda: RaisingRepository())
    configure(monkeypatch)
    monkeypatch.setenv("MT5_KILL_SWITCH_ADMIN_TOKEN", "a-different-admin-token-at-least-32-characters")
    bridge = {"X-Bridge-Key": BRIDGE}
    admin = {**bridge, "X-Kill-Switch-Admin": "a-different-admin-token-at-least-32-characters"}
    assert client.get("/v1/risk/kill-switch", headers=bridge).status_code == 503
    activate = client.post("/v1/risk/kill-switch", headers=admin,
                           json={"active": True, "actor_id": "operator", "reason": "controlled test only"})
    assert activate.status_code == 503
    permission = client.post(
        "/v1/risk/permission/check", headers=bridge,
        json=signed_body({"signal_bundle": {"permission_request": {"instrument": "EURUSD"}}}),
    )
    assert permission.status_code == 503


def test_risk_permission_route_denies_without_real_persisted_terminal_snapshot(monkeypatch):
    fake = FakeRepository()
    fake.state["kill_switch_active"] = False
    monkeypatch.setattr(api, "PostgresRepository", lambda: fake)
    configure(monkeypatch)
    monkeypatch.setattr(api, "_yaml", _policy)
    now = datetime.now(timezone.utc)
    bundle = sign_signal_bundle(make_bundle(now), now)
    response = client.post(
        "/v1/risk/permission/check", headers={"X-Bridge-Key": BRIDGE},
        json=signed_body({"signal_bundle": bundle}),
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


def test_risk_permission_route_reaches_human_review_with_a_signed_pipeline_bundle(monkeypatch):
    now = datetime.now(timezone.utc)
    bundle = make_bundle(now)
    fake = FakeRepository()
    fake.state["kill_switch_active"] = False
    fake.get_latest_mt5_risk_snapshot = lambda symbol: risk_snapshot_for(bundle, now)
    monkeypatch.setattr(api, "PostgresRepository", lambda: fake)
    monkeypatch.setattr(api, "_yaml", _policy)
    configure(monkeypatch)
    response = client.post(
        "/v1/risk/permission/check", headers={"X-Bridge-Key": BRIDGE},
        json=signed_body({"signal_bundle": sign_signal_bundle(bundle, now)}),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "REQUIRE_HUMAN"
    assert body["blockers"] == []
    assert body["details"]["permission"]["risk_reconciliation"]["status"] == "PASS"
    assert body["trade_allowed"] is False


def test_risk_permission_route_recomputes_gates_instead_of_trusting_caller_booleans(monkeypatch):
    now = datetime.now(timezone.utc)
    bundle = make_bundle(now)
    for verdict in bundle["verdicts"]:
        verdict["verdict"] = "REJECT"
    # The forged stage booleans are exactly what the old endpoint trusted.
    bundle["permission_request"].update({
        "chief_verdict": "CONFIRM", "certainty_status": "PASS", "compliance_status": "PASS",
        "risk_flags": [],
    })
    bundle["chief_reasoning"]["reasoning_hash"] = "sha256:" + "0" * 64
    fake = FakeRepository()
    fake.state["kill_switch_active"] = False
    fake.get_latest_mt5_risk_snapshot = lambda symbol: risk_snapshot_for(bundle, now)
    monkeypatch.setattr(api, "PostgresRepository", lambda: fake)
    monkeypatch.setattr(api, "_yaml", _policy)
    configure(monkeypatch)
    response = client.post(
        "/v1/risk/permission/check", headers={"X-Bridge-Key": BRIDGE},
        json=signed_body({"signal_bundle": sign_signal_bundle(bundle, now)}),
    )
    assert response.status_code == 200
    assert response.json()["status"] != "REQUIRE_HUMAN"
    assert response.json()["trade_allowed"] is False


def test_risk_permission_route_requires_a_signed_signal_bundle(monkeypatch):
    configure(monkeypatch)
    response = client.post(
        "/v1/risk/permission/check", headers={"X-Bridge-Key": BRIDGE},
        json=signed_body({"permission_request": {"instrument": "EURUSD"}}),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "DENY"
    assert "SIGNED_SIGNAL_BUNDLE_REQUIRED" in response.json()["blockers"]
