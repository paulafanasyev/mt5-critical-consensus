"""HTTP policy surface. It never dispatches or executes orders."""
from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .evidence import verify_wrapper
from .gates import (
    certainty_gate,
    chief_skeptic_pass,
    permission_gate,
)
from .governance import apply_attested_calibration, verify_governance_attestation
from .persistence import PostgresRepository
from .risk_controls import risk_controlled_permission

ROOT = Path(os.getenv("POLICY_DIR", str(Path(__file__).resolve().parents[2] / "config")))


def _yaml(name: str) -> dict:
    path = ROOT / name
    try:
        with path.open("r", encoding="utf-8") as file:
            value = yaml.safe_load(file) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise HTTPException(status_code=503, detail="Policy configuration unavailable") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=503, detail="Policy configuration invalid")
    return value


def require_bridge_key(x_bridge_key: str | None = Header(default=None, alias="X-Bridge-Key")) -> None:
    """Shared-secret transport gate for the localhost bridge; not a user-signature verifier."""
    expected = os.getenv("MT5_BRIDGE_TOKEN", "")
    if len(expected) < 24:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bridge authentication is not configured; protected API disabled",
        )
    if not x_bridge_key or not hmac.compare_digest(x_bridge_key.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bridge credentials")


app = FastAPI(
    title="Critical Consensus Policy API",
    version="0.3.0",
    description="Policy checks only; order execution is disabled in this reference scaffold.",
)


def _key_configured(name: str) -> bool:
    return 32 <= len(os.getenv(name, "").encode("utf-8")) <= 256


def _required_key(name: str) -> str:
    key = os.getenv(name, "")
    if not _key_configured(name):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{name} is not configured; protected evaluation disabled",
        )
    return key


def _required_issuer(name: str) -> str:
    issuer = os.getenv(name, "").strip()
    if not issuer:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{name} is not configured; issuer pinning is required",
        )
    return issuer


def _evidence_configuration() -> tuple[str, str]:
    return _required_key("MT5_EVIDENCE_HMAC_KEY"), _required_issuer("MT5_EVIDENCE_ISSUER")


def _governance_configuration() -> tuple[str, str]:
    return _required_key("MT5_GOVERNANCE_HMAC_KEY"), _required_issuer("MT5_GOVERNANCE_ISSUER")


def _assert_separated_domains() -> None:
    """Evidence ingress and calibration governance must not share key material or issuer."""
    evidence_key, evidence_issuer = _evidence_configuration()
    governance_key, governance_issuer = _governance_configuration()
    if evidence_key == governance_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Evidence and governance HMAC keys must be different; configuration rejected",
        )
    if evidence_issuer == governance_issuer:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Evidence and governance issuers must be different; configuration rejected",
        )


class SignedPayload(BaseModel):
    payload: dict[str, Any]
    envelope: dict[str, Any]


class CertaintyRequest(SignedPayload):
    governance_attestation: dict[str, Any] | None = None


class KillSwitchPayload(BaseModel):
    active: bool
    actor_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)


def _verified_payload(body: SignedPayload) -> dict[str, Any]:
    """Reject payloads that do not carry a fresh trusted-ingress envelope."""
    key, issuer = _evidence_configuration()
    check = verify_wrapper(
        {"payload": body.payload, "envelope": body.envelope},
        key=key,
        expected_issuer=issuer,
    )
    if check.get("valid") is not True:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "evidence_envelope_invalid", "blockers": check.get("blockers", [])},
        )
    return body.payload


@app.get("/health")
def health() -> dict[str, Any]:
    """Non-sensitive liveness endpoint; reports the execution state, never credentials."""
    bridge_ready = len(os.getenv("MT5_BRIDGE_TOKEN", "")) >= 24
    evidence_key_ready = _key_configured("MT5_EVIDENCE_HMAC_KEY")
    governance_key_ready = _key_configured("MT5_GOVERNANCE_HMAC_KEY")
    keys_distinct = (
        evidence_key_ready and governance_key_ready
        and os.getenv("MT5_EVIDENCE_HMAC_KEY") != os.getenv("MT5_GOVERNANCE_HMAC_KEY")
    )
    evidence_issuer = os.getenv("MT5_EVIDENCE_ISSUER", "").strip()
    governance_issuer = os.getenv("MT5_GOVERNANCE_ISSUER", "").strip()
    return {
        "status": "ok",
        "service": "policy-api",
        "execution_enabled": False,
        "live_enabled": False,
        "authenticated_policy_endpoints": bridge_ready,
        "evidence_verification_configured": evidence_key_ready and bool(evidence_issuer),
        "governance_verification_configured": governance_key_ready and bool(governance_issuer),
        "signing_keys_distinct": keys_distinct,
        "signing_issuers_distinct": bool(evidence_issuer) and bool(governance_issuer)
                                   and evidence_issuer != governance_issuer,
    }


@app.post("/v1/certainty/evaluate", dependencies=[Depends(require_bridge_key)])
def evaluate_certainty(body: CertaintyRequest) -> dict[str, Any]:
    _assert_separated_domains()
    payload = _verified_payload(body)
    governance_key, governance_issuer = _governance_configuration()
    governance = verify_governance_attestation(
        body.governance_attestation, key=governance_key,
        expected_issuer=governance_issuer,
    )
    if governance.get("valid") is not True:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "governance_attestation_invalid",
                    "blockers": governance.get("blockers", [])},
        )
    evaluated = dict(payload)
    raw_verdicts = payload.get("verdicts", [])
    evaluated["verdicts"] = apply_attested_calibration(
        raw_verdicts if isinstance(raw_verdicts, list) else [],
        governance, issuer=governance_issuer,
    )
    return certainty_gate(evaluated, _yaml("certainty_thresholds.yaml"), governance=governance)


@app.post("/v1/chief/review", dependencies=[Depends(require_bridge_key)])
def review_chief(body: SignedPayload) -> dict[str, Any]:
    return chief_skeptic_pass(_verified_payload(body))


@app.post("/v1/permission/check", dependencies=[Depends(require_bridge_key)])
def check_permission(body: SignedPayload) -> dict[str, Any]:
    return permission_gate(_verified_payload(body), _yaml("permission_matrix.yaml"))


@app.get("/v1/risk/kill-switch", dependencies=[Depends(require_bridge_key)])
def get_kill_switch() -> dict[str, Any]:
    """Return persisted emergency-stop state; unavailable DB is never treated as disabled."""
    try:
        return PostgresRepository().get_kill_switch_state()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Persisted kill-switch state unavailable; fail closed") from exc


@app.post("/v1/risk/kill-switch", dependencies=[Depends(require_bridge_key)])
def update_kill_switch(
    body: KillSwitchPayload,
    x_kill_switch_admin: str | None = Header(default=None, alias="X-Kill-Switch-Admin"),
) -> dict[str, Any]:
    """Activate the emergency stop with bridge auth; disabling additionally requires a separate admin secret."""
    if not body.active:
        expected = os.getenv("MT5_KILL_SWITCH_ADMIN_TOKEN", "")
        if len(expected) < 32:
            raise HTTPException(status_code=503, detail="Separate kill-switch admin token is not configured; switch cannot be disabled")
        if not x_kill_switch_admin or not hmac.compare_digest(
            x_kill_switch_admin.encode("utf-8"), expected.encode("utf-8")
        ):
            raise HTTPException(status_code=403, detail="Separate kill-switch administrator authentication required")
    try:
        return PostgresRepository().set_kill_switch(
            active=body.active, actor_id=body.actor_id, reason=body.reason
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Kill-switch persistence failed; fail closed") from exc


@app.post("/v1/risk/permission/check", dependencies=[Depends(require_bridge_key)])
def check_risk_controlled_permission(body: SignedPayload) -> dict[str, Any]:
    """Policy check with persisted global kill-switch and fresh terminal risk snapshot."""
    payload = _verified_payload(body)
    request_payload = payload.get("permission_request", payload)
    if not isinstance(request_payload, dict):
        return {"status": "DENY", "allowed": False, "trade_allowed": False,
                "blockers": ["INVALID_PERMISSION_REQUEST"]}
    symbol = str(request_payload.get("instrument", "")).strip().upper()
    if not symbol:
        return {"status": "DENY", "allowed": False, "trade_allowed": False,
                "blockers": ["INSTRUMENT_MISSING"]}
    try:
        repo = PostgresRepository()
        kill_switch = repo.get_kill_switch_state()
        snapshot = repo.get_latest_mt5_risk_snapshot(symbol)
        return risk_controlled_permission(
            request_payload, _yaml("permission_matrix.yaml"),
            kill_switch_state=kill_switch, snapshot=snapshot,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Risk controls unavailable; request denied") from exc


@app.post("/v1/authorization/check", dependencies=[Depends(require_bridge_key)])
def check_authorization() -> None:
    # No durable permission store or cryptographic 2FA/signature verifier exists in this scaffold.
    # Never treat a client-supplied `permission_result` object as proof that permission was granted.
    raise HTTPException(
        status_code=status.HTTP_423_LOCKED,
        detail="Authorization endpoint disabled: server-side permission state and real 2FA/signature verification are not implemented",
    )


@app.get("/v1/execution/next", status_code=204, dependencies=[Depends(require_bridge_key)])
def next_execution_command():
    # Intentionally no queue consumer; no order command can be fetched from this scaffold.
    return None


@app.post("/v1/execution/submit", dependencies=[Depends(require_bridge_key)])
def submit_execution() -> None:
    raise HTTPException(
        status_code=status.HTTP_423_LOCKED,
        detail="Execution disabled in reference scaffold; no order was sent",
    )
