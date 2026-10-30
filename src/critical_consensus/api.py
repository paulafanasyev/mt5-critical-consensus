"""HTTP policy surface. It never dispatches or executes orders."""
from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .audit import canonical_hash
from .evidence import verify_review, verify_wrapper
from .gates import (
    certainty_gate,
    chief_skeptic_pass,
    revalidate_review_evidence,
)
from .calibration import weighted_consensus
from .governance import apply_attested_calibration, verify_governance_attestation
from .persistence import PostgresRepository
from .pipeline import (
    _validate_verdict_metadata,
    evaluate_signal_bundle,
    review_attestation_payload,
)

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


SIGNING_KEY_ENV = ("MT5_EVIDENCE_HMAC_KEY", "MT5_GOVERNANCE_HMAC_KEY", "MT5_OPERATOR_HMAC_KEY",
                   "MT5_REVIEW_HMAC_KEY")


def _secret_reuses_signing_key(token: str) -> str | None:
    """Return the signing-domain env name that shares this secret, if any."""
    for name in SIGNING_KEY_ENV:
        other = os.getenv(name, "")
        if other and hmac.compare_digest(token.encode("utf-8"), other.encode("utf-8")):
            return name
    return None


def _bridge_token_reuses_signing_key(token: str) -> str | None:
    return _secret_reuses_signing_key(token)


def _kill_switch_admin_reuses_secret(token: str) -> str | None:
    """The emergency-stop disable secret must be independent of every request credential."""
    bridge = os.getenv("MT5_BRIDGE_TOKEN", "")
    if bridge and hmac.compare_digest(token.encode("utf-8"), bridge.encode("utf-8")):
        return "MT5_BRIDGE_TOKEN"
    return _secret_reuses_signing_key(token)


def require_bridge_key(x_bridge_key: str | None = Header(default=None, alias="X-Bridge-Key")) -> None:
    """Shared-secret transport gate for the localhost bridge; not a user-signature verifier."""
    expected = os.getenv("MT5_BRIDGE_TOKEN", "")
    if len(expected) < 24:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bridge authentication is not configured; protected API disabled",
        )
    reused = _bridge_token_reuses_signing_key(expected)
    if reused:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"MT5_BRIDGE_TOKEN must not equal {reused}; configuration rejected",
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


def _review_configuration() -> tuple[str, str]:
    return _required_key("MT5_REVIEW_HMAC_KEY"), _required_issuer("MT5_REVIEW_ISSUER")


def _assert_separated_domains() -> None:
    """Ingress, governance and review-service signing material must never be shared."""
    evidence_key, evidence_issuer = _evidence_configuration()
    governance_key, governance_issuer = _governance_configuration()
    review_key, review_issuer = _review_configuration()
    if evidence_key == governance_key or review_key in (evidence_key, governance_key):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Evidence, governance and review HMAC keys must be different; configuration rejected",
        )
    if evidence_issuer == governance_issuer or review_issuer in (evidence_issuer, governance_issuer):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Evidence, governance and review issuers must be different; configuration rejected",
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


def _require_review_attestation(payload: Mapping[str, Any]) -> None:
    """Peer reviews and Chief reasoning are only trusted when the review service signed them."""
    key, issuer = _review_configuration()
    check = verify_review(review_attestation_payload(payload), payload.get("review_attestation"),
                          key=key, expected_issuer=issuer)
    if check.get("valid") is not True:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "review_attestation_invalid", "blockers": check.get("blockers", [])},
        )


@app.get("/health")
def health() -> dict[str, Any]:
    """Non-sensitive liveness endpoint; reports the execution state, never credentials."""
    bridge_token = os.getenv("MT5_BRIDGE_TOKEN", "")
    bridge_token_distinct = bool(bridge_token) and _bridge_token_reuses_signing_key(bridge_token) is None
    bridge_ready = len(bridge_token) >= 24 and bridge_token_distinct
    evidence_key = os.getenv("MT5_EVIDENCE_HMAC_KEY", "")
    governance_key = os.getenv("MT5_GOVERNANCE_HMAC_KEY", "")
    operator_key = os.getenv("MT5_OPERATOR_HMAC_KEY", "")
    review_key = os.getenv("MT5_REVIEW_HMAC_KEY", "")
    evidence_key_ready = _key_configured("MT5_EVIDENCE_HMAC_KEY")
    governance_key_ready = _key_configured("MT5_GOVERNANCE_HMAC_KEY")
    operator_key_ready = _key_configured("MT5_OPERATOR_HMAC_KEY")
    review_key_ready = _key_configured("MT5_REVIEW_HMAC_KEY")
    configured_keys = [key for key, ready in (
        (evidence_key, evidence_key_ready), (governance_key, governance_key_ready),
        (operator_key, operator_key_ready), (review_key, review_key_ready),
    ) if ready]
    keys_distinct = (
        evidence_key_ready and governance_key_ready and review_key_ready
        and len(set(configured_keys)) == len(configured_keys)
    )
    evidence_issuer = os.getenv("MT5_EVIDENCE_ISSUER", "").strip()
    governance_issuer = os.getenv("MT5_GOVERNANCE_ISSUER", "").strip()
    operator_issuer = os.getenv("MT5_OPERATOR_ISSUER", "").strip()
    review_issuer = os.getenv("MT5_REVIEW_ISSUER", "").strip()
    operator_key_distinct = (
        operator_key_ready and operator_key not in (evidence_key, governance_key, review_key, bridge_token)
    )
    operator_issuer_distinct = (
        bool(operator_issuer) and operator_issuer not in (evidence_issuer, governance_issuer, review_issuer)
    )
    review_key_distinct = (
        review_key_ready and review_key not in (evidence_key, governance_key, operator_key, bridge_token)
    )
    review_issuer_distinct = (
        bool(review_issuer) and review_issuer not in (evidence_issuer, governance_issuer, operator_issuer)
    )
    admin_token = os.getenv("MT5_KILL_SWITCH_ADMIN_TOKEN", "")
    admin_distinct = len(admin_token) >= 32 and _kill_switch_admin_reuses_secret(admin_token) is None
    evaluation_ready = (bridge_ready and keys_distinct and bool(evidence_issuer)
                        and bool(governance_issuer) and bool(review_issuer))
    configured_issuers = [issuer for issuer in
                          (evidence_issuer, governance_issuer, review_issuer, operator_issuer) if issuer]
    return {
        "status": "ok",
        "service": "policy-api",
        "execution_enabled": False,
        "live_enabled": False,
        "authenticated_policy_endpoints": evaluation_ready,
        "bridge_token_distinct_from_signing_keys": bridge_token_distinct,
        "kill_switch_admin_distinct_from_request_secrets": admin_distinct,
        "evidence_verification_configured": evidence_key_ready and bool(evidence_issuer),
        "governance_verification_configured": governance_key_ready and bool(governance_issuer),
        "operator_verification_configured": operator_key_ready and bool(operator_issuer),
        "review_verification_configured": review_key_ready and bool(review_issuer),
        "operator_key_distinct": operator_key_distinct,
        "operator_issuer_distinct": operator_issuer_distinct,
        "review_key_distinct": review_key_distinct,
        "review_issuer_distinct": review_issuer_distinct,
        "signing_keys_distinct": keys_distinct,
        "signing_issuers_distinct": all((evidence_issuer, governance_issuer, review_issuer))
                                    and len(set(configured_issuers)) == len(configured_issuers),
    }


def _verdict_metadata_blockers(payload: Mapping[str, Any]) -> list[str]:
    """Bind every verdict to the signed signal and reject stale or unbound ones.

    The pipeline performs the same checks; without them this endpoint would be a weaker
    path where a verdict written for another signal, or an expired verdict, still
    reaches the certainty gate.
    """
    current = datetime.now(timezone.utc)
    signal_id = str(payload.get("signal_id", "")).strip()
    blockers: list[str] = []
    if not signal_id:
        blockers.append("SIGNAL_ID_MISSING")
    signal_input = payload.get("signal_input")
    supplied_hash = payload.get("signal_input_hash")
    expected_hash: str | None = None
    if not isinstance(signal_input, Mapping) or not signal_id:
        blockers.append("SIGNAL_INPUT_OR_HASH_MISSING")
    else:
        if str(signal_input.get("signal_id", "")).strip() != signal_id:
            blockers.append("SIGNAL_INPUT_SIGNAL_ID_MISMATCH")
        try:
            expected_hash = canonical_hash(dict(signal_input))
        except (TypeError, ValueError, OverflowError):
            blockers.append("SIGNAL_INPUT_NOT_CANONICALLY_HASHABLE")
        if expected_hash is not None and supplied_hash != expected_hash:
            blockers.append("SIGNAL_INPUT_HASH_MISMATCH")
    raw_verdicts = payload.get("verdicts")
    verdicts = raw_verdicts if isinstance(raw_verdicts, list) else []
    for index, verdict in enumerate(verdicts):
        if not isinstance(verdict, Mapping):
            blockers.append(f"VERDICT_{index}:VERDICT_RECORD_INVALID")
            continue
        blockers.extend(f"VERDICT_{index}:{blocker}"
                        for blocker in _validate_verdict_metadata(verdict, signal_id, current))
        if expected_hash is not None and verdict.get("input_hash") != expected_hash:
            blockers.append(f"VERDICT_{index}:VERDICT_INPUT_HASH_DOES_NOT_MATCH_SIGNAL_INPUT")
    return blockers


@app.post("/v1/certainty/evaluate", dependencies=[Depends(require_bridge_key)])
def evaluate_certainty(body: CertaintyRequest) -> dict[str, Any]:
    _assert_separated_domains()
    payload = _verified_payload(body)
    _require_review_attestation(payload)
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
    thresholds = _yaml("certainty_thresholds.yaml")
    review_evidence = revalidate_review_evidence(
        evaluated, evaluated["verdicts"],
        min_reviewers_per_verdict=thresholds.get("min_reviewers_per_verdict", 2)
        if isinstance(thresholds.get("min_reviewers_per_verdict"), int) else 2,
    )
    evidence_blockers = list(review_evidence.get("blockers", []))
    evidence_blockers.extend(_verdict_metadata_blockers(evaluated))
    verdicts_with_counts = []
    for verdict in evaluated["verdicts"]:
        if not isinstance(verdict, dict):
            verdicts_with_counts.append(verdict)
            continue
        verdict_id = str(verdict.get("verdict_id", "")).strip()
        verdicts_with_counts.append({**verdict,
                                     "reviewer_count": review_evidence["reviewer_counts"].get(verdict_id, 0)})
    evaluated["verdicts"] = verdicts_with_counts
    try:
        recomputed_consensus = weighted_consensus(verdicts_with_counts)
    except (TypeError, ValueError, KeyError, OverflowError):
        recomputed_consensus = 0.0
        evidence_blockers.append("CONSENSUS_SCORE_COULD_NOT_BE_RECOMPUTED")
    supplied_consensus = evaluated.get("consensus_score")
    if (isinstance(supplied_consensus, bool) or not isinstance(supplied_consensus, (int, float))
            or abs(float(supplied_consensus) - recomputed_consensus) > 1e-9):
        evidence_blockers.append("CONSENSUS_SCORE_DOES_NOT_MATCH_VERDICTS")
    evaluated["consensus_score"] = recomputed_consensus
    if review_evidence["open_challenges"] is not None:
        evaluated["open_challenges"] = [{"challenge_id": challenge_id}
                                        for challenge_id in review_evidence["open_challenges"]]
    raw_flags = evaluated.get("risk_flags", [])
    flags = list(raw_flags) if isinstance(raw_flags, list) else []
    evaluated["risk_flags"] = flags + evidence_blockers
    return certainty_gate(evaluated, thresholds, governance=governance)


@app.post("/v1/chief/review", dependencies=[Depends(require_bridge_key)])
def review_chief(body: SignedPayload) -> dict[str, Any]:
    _assert_separated_domains()
    payload = _verified_payload(body)
    _require_review_attestation(payload)
    return chief_skeptic_pass(payload)


@app.post("/v1/permission/check", dependencies=[Depends(require_bridge_key)])
def check_permission() -> None:
    # Caller-supplied risk aggregates are not terminal measurements. Use the persisted
    # kill-switch and terminal snapshot endpoint instead; this route stays disabled.
    raise HTTPException(
        status_code=status.HTTP_423_LOCKED,
        detail="Permission check disabled: caller-supplied risk aggregates are not trusted; use /v1/risk/permission/check",
    )


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
        reused = _kill_switch_admin_reuses_secret(expected)
        if reused:
            raise HTTPException(
                status_code=503,
                detail=f"MT5_KILL_SWITCH_ADMIN_TOKEN must not equal {reused}; configuration rejected",
            )
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
    """Re-run the whole gate chain and reconcile a persisted terminal risk snapshot.

    Caller-declared ``chief_verdict``/``certainty_status``/``compliance_status`` strings are
    never evidence: they are recomputed from the signed bundle before the risk gate runs.
    """
    _assert_separated_domains()
    payload = _verified_payload(body)
    bundle = payload.get("signal_bundle")
    if not isinstance(bundle, Mapping):
        return {"status": "DENY", "allowed": False, "trade_allowed": False,
                "blockers": ["SIGNED_SIGNAL_BUNDLE_REQUIRED"]}
    bundle = dict(bundle)
    request_payload = bundle.get("permission_request")
    symbol = (str(request_payload.get("instrument", "")).strip().upper()
              if isinstance(request_payload, Mapping) else "")
    if not symbol:
        return {"status": "DENY", "allowed": False, "trade_allowed": False,
                "blockers": ["INSTRUMENT_MISSING"]}
    evidence_key, evidence_issuer = _evidence_configuration()
    governance_key, governance_issuer = _governance_configuration()
    review_key, review_issuer = _review_configuration()
    try:
        repo = PostgresRepository()
        kill_switch = repo.get_kill_switch_state()
        snapshot = repo.get_latest_mt5_risk_snapshot(symbol)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Risk controls unavailable; request denied") from exc
    result = evaluate_signal_bundle(
        bundle, _yaml("certainty_thresholds.yaml"), _yaml("permission_matrix.yaml"),
        require_persisted_risk=True,
        kill_switch_state=kill_switch, terminal_risk_snapshot=snapshot,
        evidence_key=evidence_key, governance_key=governance_key,
        expected_evidence_issuer=evidence_issuer, expected_governance_issuer=governance_issuer,
        operator_key=os.getenv("MT5_OPERATOR_HMAC_KEY", "") or None,
        expected_operator_issuer=os.getenv("MT5_OPERATOR_ISSUER", "").strip() or None,
        review_key=review_key, expected_review_issuer=review_issuer,
    )
    # Approval still requires a durable nonce/permission store, which this route does not own.
    if bundle.get("user_decision") is not None:
        result["blockers"] = sorted(set(result.get("blockers", [])) | {"AUTHORIZATION_REQUIRES_DURABLE_STORE"})
        result["status"] = "DENY"
    result["trade_allowed"] = False
    return result


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
