"""HTTP policy surface. It never dispatches or executes orders."""
from __future__ import annotations

from datetime import datetime, timezone
import hmac
import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .audit import canonical_hash
from .contracts import validate_contract
from .gates import certainty_gate, chief_skeptic_pass, permission_gate

ROOT = Path(os.getenv("POLICY_DIR", str(Path(__file__).resolve().parents[2] / "config")))
CHIEF_MAX_AGE_SECONDS = 300
CHIEF_FUTURE_SKEW_SECONDS = 30


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
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Bridge authentication is not configured; protected API disabled")
    if not x_bridge_key or not hmac.compare_digest(x_bridge_key.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bridge credentials")


app = FastAPI(title="Critical Consensus Policy API", version="0.2.0",
              description="Policy checks only; order execution is disabled in this reference scaffold.")


class GenericPayload(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


@app.get("/health")
def health() -> dict[str, Any]:
    """Non-sensitive liveness endpoint; reports the execution state, never credentials."""
    return {"status": "ok", "service": "policy-api", "execution_enabled": False,
            "live_enabled": False, "authenticated_policy_endpoints": True}


@app.post("/v1/certainty/evaluate", dependencies=[Depends(require_bridge_key)])
def evaluate_certainty(body: GenericPayload) -> dict[str, Any]:
    return certainty_gate(body.payload, _yaml("certainty_thresholds.yaml"))


@app.post("/v1/chief/review", dependencies=[Depends(require_bridge_key)])
def review_chief(body: GenericPayload) -> dict[str, Any]:
    """Review only complete, self-consistent, fresh Chief reasoning records."""
    reasoning = body.payload
    created_raw = reasoning.get("created_at")
    if not isinstance(created_raw, str) or not created_raw.strip():
        return {"verdict": "HOLD", "passed": False, "trade_allowed": False,
                "blockers": ["CHIEF_CREATED_AT_MISSING_OR_INVALID"]}
    try:
        created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("timezone required")
        created_at = created_at.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return {"verdict": "HOLD", "passed": False, "trade_allowed": False,
                "blockers": ["CHIEF_CREATED_AT_MISSING_OR_INVALID"]}
    age = (datetime.now(timezone.utc) - created_at).total_seconds()
    if age > CHIEF_MAX_AGE_SECONDS:
        return {"verdict": "HOLD", "passed": False, "trade_allowed": False,
                "blockers": ["CHIEF_REASONING_EXPIRED"]}
    if age < -CHIEF_FUTURE_SKEW_SECONDS:
        return {"verdict": "HOLD", "passed": False, "trade_allowed": False,
                "blockers": ["CHIEF_CREATED_AT_IN_FUTURE"]}
    supplied_hash = reasoning.get("reasoning_hash")
    if not isinstance(supplied_hash, str) or not supplied_hash.strip():
        return {"verdict": "HOLD", "passed": False, "trade_allowed": False,
                "blockers": ["CHIEF_REASONING_HASH_MISSING_OR_INVALID"]}
    unsigned = dict(reasoning)
    unsigned.pop("reasoning_hash", None)
    if supplied_hash != canonical_hash(unsigned):
        return {"verdict": "HOLD", "passed": False, "trade_allowed": False,
                "blockers": ["CHIEF_REASONING_HASH_MISMATCH"]}
    valid, _ = validate_contract("chief_reasoning.schema.json", reasoning)
    if not valid:
        return {"verdict": "HOLD", "passed": False, "trade_allowed": False,
                "blockers": ["CHIEF_REASONING_SCHEMA_INVALID"]}
    return chief_skeptic_pass(reasoning)


@app.post("/v1/permission/check", dependencies=[Depends(require_bridge_key)])
def check_permission(body: GenericPayload) -> dict[str, Any]:
    return permission_gate(body.payload, _yaml("permission_matrix.yaml"))


@app.post("/v1/authorization/check", dependencies=[Depends(require_bridge_key)])
def check_authorization() -> None:
    # No durable permission store or cryptographic 2FA/signature verifier exists in this scaffold.
    # Never treat a client-supplied `permission_result` object as proof that permission was granted.
    raise HTTPException(status_code=status.HTTP_423_LOCKED,
                        detail="Authorization endpoint disabled: server-side permission state and real 2FA/signature verification are not implemented")


@app.get("/v1/execution/next", status_code=204, dependencies=[Depends(require_bridge_key)])
def next_execution_command():
    # Intentionally no queue consumer; no order command can be fetched from this scaffold.
    return None


@app.post("/v1/execution/submit", dependencies=[Depends(require_bridge_key)])
def submit_execution() -> None:
    raise HTTPException(status_code=status.HTTP_423_LOCKED,
                        detail="Execution disabled in reference scaffold; no order was sent")
