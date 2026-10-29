"""Transactional PostgreSQL one-time decision registry.

This module is a storage primitive only. Call register_verified_decision exclusively
from trusted server-side code after a real identity/MFA/signature verifier succeeds.
It is deliberately not wired to the public authorization API; storage registration is
not itself proof of user identity and does not authorize or dispatch an order.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from .audit import canonical_hash

MAX_DECISION_TTL_SECONDS = 300
_ALLOWED_METHODS = {"mfa", "totp", "webauthn", "signed_challenge"}
_ALLOWED_FIELDS = {
    "decision_id", "permission_id", "user_id", "decision", "timestamp", "reason",
    "ttl_seconds", "two_fa_verified", "signature_verified", "ip_hash",
}
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS user_decisions (
    decision_id TEXT PRIMARY KEY CHECK (btrim(decision_id) <> ''),
    permission_id TEXT NOT NULL CHECK (btrim(permission_id) <> ''),
    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
    decision TEXT NOT NULL CHECK (decision IN ('APPROVE', 'REJECT')),
    mode TEXT NOT NULL CHECK (mode IN ('demo', 'live')),
    decision_timestamp TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    payload_hash TEXT NOT NULL,
    verifier_id TEXT NOT NULL,
    verification_method TEXT NOT NULL,
    verification_ref TEXT NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (expires_at > decision_timestamp)
)
"""
_CREATE_PERMISSION_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_decisions_permission
ON user_decisions (permission_id)
"""


class DecisionStoreError(RuntimeError):
    """A storage failure that must be treated as DENY by callers."""


@dataclass(frozen=True)
class VerifiedAuthReceipt:
    """Server-side receipt returned by a trusted authentication verifier.

    Do not construct this from client JSON. The calling authentication service must
    verify the real session/challenge and, for live mode, the required second factor
    and signature before producing this receipt.
    """
    verifier_id: str
    method: str
    receipt_id: str
    verified_at: str
    two_fa_verified: bool = False
    signature_verified: bool = False


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _current_time(value: datetime | None) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("timezone-aware current time required")
    return current.astimezone(timezone.utc)


def _required_text(payload: Mapping[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _decision_metadata(decision: Mapping[str, Any], current: datetime) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(decision, Mapping):
        return None, "DECISION_PAYLOAD_INVALID"
    if set(decision) - _ALLOWED_FIELDS:
        return None, "DECISION_FIELDS_INVALID"
    for field in ("decision_id", "permission_id", "user_id", "reason"):
        if _required_text(decision, field) is None:
            return None, f"{field.upper()}_INVALID"
    if decision.get("decision") not in {"APPROVE", "REJECT"}:
        return None, "DECISION_VALUE_INVALID"
    ttl = decision.get("ttl_seconds")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= MAX_DECISION_TTL_SECONDS:
        return None, "DECISION_TTL_INVALID"
    decision_at = _parse_time(decision.get("timestamp"))
    if decision_at is None:
        return None, "DECISION_TIMESTAMP_INVALID"
    age = (current - decision_at).total_seconds()
    if age < -30 or age > ttl:
        return None, "DECISION_STALE_OR_FUTURE"
    for field in ("two_fa_verified", "signature_verified"):
        if field in decision and not isinstance(decision[field], bool):
            return None, "DECISION_VERIFICATION_FIELDS_INVALID"
    if "ip_hash" in decision and not isinstance(decision["ip_hash"], str):
        return None, "DECISION_IP_HASH_INVALID"
    return {"decision_at": decision_at, "ttl": ttl}, None


class PostgresDecisionStore:
    """Atomic, one-time decision storage backed by PostgreSQL.

    Connection factory must return a new psycopg-compatible connection for each call.
    All storage errors raise DecisionStoreError. Callers must deny on that exception.
    """

    def __init__(self, connection_factory: Callable[[], Any], *, max_ttl_seconds: int = MAX_DECISION_TTL_SECONDS):
        if not callable(connection_factory):
            raise ValueError("connection_factory is required")
        if isinstance(max_ttl_seconds, bool) or not isinstance(max_ttl_seconds, int) or not 1 <= max_ttl_seconds <= MAX_DECISION_TTL_SECONDS:
            raise ValueError("max_ttl_seconds must be in 1..300")
        self._connect = connection_factory
        self._max_ttl = max_ttl_seconds

    def ensure_schema(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute(_CREATE_TABLE)
                connection.execute(_CREATE_PERMISSION_INDEX)
        except Exception as exc:
            raise DecisionStoreError("authorization storage is unavailable") from exc

    def register_verified_decision(
        self,
        decision: Mapping[str, Any],
        receipt: VerifiedAuthReceipt,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Register a verifier-approved decision only while its permission is pending."""
        try:
            current = _current_time(now)
        except (ValueError, TypeError, OverflowError):
            return {"registered": False, "blockers": ["CURRENT_TIME_INVALID"], "trade_allowed": False}
        metadata, blocker = _decision_metadata(decision, current)
        if blocker:
            return {"registered": False, "blockers": [blocker], "trade_allowed": False}
        if metadata is None:
            return {"registered": False, "blockers": ["DECISION_PAYLOAD_INVALID"], "trade_allowed": False}
        if not isinstance(receipt, VerifiedAuthReceipt):
            return {"registered": False, "blockers": ["VERIFIED_AUTH_RECEIPT_REQUIRED"], "trade_allowed": False}
        for field in ("verifier_id", "method", "receipt_id"):
            if not isinstance(getattr(receipt, field), str) or not getattr(receipt, field).strip():
                return {"registered": False, "blockers": ["VERIFIED_AUTH_RECEIPT_INVALID"], "trade_allowed": False}
        if receipt.method not in _ALLOWED_METHODS:
            return {"registered": False, "blockers": ["VERIFICATION_METHOD_INVALID"], "trade_allowed": False}
        if not isinstance(receipt.two_fa_verified, bool) or not isinstance(receipt.signature_verified, bool):
            return {"registered": False, "blockers": ["VERIFIED_AUTH_RECEIPT_INVALID"], "trade_allowed": False}
        verified_at = _parse_time(receipt.verified_at)
        if verified_at is None or abs((verified_at - metadata["decision_at"]).total_seconds()) > 30:
            return {"registered": False, "blockers": ["VERIFIED_AUTH_TIMESTAMP_INVALID"], "trade_allowed": False}
        if verified_at > current + timedelta(seconds=30):
            return {"registered": False, "blockers": ["VERIFIED_AUTH_TIMESTAMP_IN_FUTURE"], "trade_allowed": False}
        decision_id = _required_text(decision, "decision_id")
        permission_id = _required_text(decision, "permission_id")
        user_id = _required_text(decision, "user_id")
        if decision.get("decision") == "APPROVE" and not isinstance(decision.get("two_fa_verified", False), bool):
            return {"registered": False, "blockers": ["DECISION_VERIFICATION_FIELDS_INVALID"], "trade_allowed": False}

        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT status, mode, expires_at FROM permissions WHERE permission_id::text = %s FOR UPDATE",
                    (permission_id,),
                ).fetchone()
                if row is None:
                    return {"registered": False, "blockers": ["PERMISSION_NOT_FOUND"], "trade_allowed": False}
                status_value, mode, permission_expiry = row
                if status_value != "REQUIRE_HUMAN" or mode not in {"demo", "live"}:
                    return {"registered": False, "blockers": ["PERMISSION_NOT_PENDING_HUMAN_APPROVAL"], "trade_allowed": False}
                if permission_expiry.tzinfo is None:
                    return {"registered": False, "blockers": ["PERMISSION_EXPIRY_INVALID"], "trade_allowed": False}
                permission_expiry = permission_expiry.astimezone(timezone.utc)
                expires_at = min(metadata["decision_at"] + timedelta(seconds=min(metadata["ttl"], self._max_ttl)), permission_expiry)
                if permission_expiry <= current or expires_at <= current or expires_at <= metadata["decision_at"]:
                    return {"registered": False, "blockers": ["PERMISSION_OR_DECISION_EXPIRED"], "trade_allowed": False}
                if mode == "live" and decision.get("decision") == "APPROVE":
                    if receipt.two_fa_verified is not True or receipt.signature_verified is not True:
                        return {"registered": False, "blockers": ["LIVE_2FA_OR_SIGNATURE_VERIFICATION_REQUIRED"], "trade_allowed": False}
                    if decision.get("two_fa_verified") is not True or decision.get("signature_verified") is not True:
                        return {"registered": False, "blockers": ["DECISION_LIVE_VERIFICATION_FLAGS_MISSING"], "trade_allowed": False}
                result = connection.execute(
                    """
                    INSERT INTO user_decisions (
                        decision_id, permission_id, user_id, decision, mode, decision_timestamp,
                        expires_at, payload_hash, verifier_id, verification_method,
                        verification_ref, verified_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING decision_id
                    """,
                    (decision_id, permission_id, user_id, decision["decision"], mode,
                     metadata["decision_at"], expires_at, canonical_hash(dict(decision)),
                     receipt.verifier_id.strip(), receipt.method, receipt.receipt_id.strip(), verified_at),
                ).fetchone()
                if result is None:
                    return {"registered": False, "blockers": ["DECISION_OR_PERMISSION_ALREADY_REGISTERED"], "trade_allowed": False}
                return {"registered": True, "decision_id": decision_id, "expires_at": expires_at.isoformat(),
                        "blockers": [], "trade_allowed": False}
        except DecisionStoreError:
            raise
        except Exception as exc:
            raise DecisionStoreError("authorization storage is unavailable") from exc

    def consume_decision(
        self,
        permission_result: Mapping[str, Any],
        decision: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically consume a matching server-registered decision once; never authorizes an order."""
        try:
            current = _current_time(now)
        except (ValueError, TypeError, OverflowError):
            return {"consumed": False, "status": "DENY", "blockers": ["CURRENT_TIME_INVALID"], "trade_allowed": False}
        metadata, blocker = _decision_metadata(decision, current)
        if blocker or metadata is None:
            return {"consumed": False, "status": "DENY", "blockers": [blocker or "DECISION_PAYLOAD_INVALID"], "trade_allowed": False}
        if not isinstance(permission_result, Mapping) or permission_result.get("status") != "REQUIRE_HUMAN":
            return {"consumed": False, "status": "DENY", "blockers": ["PERMISSION_NOT_PENDING_HUMAN_APPROVAL"], "trade_allowed": False}
        decision_id = _required_text(decision, "decision_id")
        permission_id = _required_text(decision, "permission_id")
        if permission_result.get("permission_id") != permission_id:
            return {"consumed": False, "status": "DENY", "blockers": ["PERMISSION_ID_MISMATCH"], "trade_allowed": False}
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    UPDATE user_decisions AS d
                    SET consumed_at = %s
                    WHERE d.decision_id = %s
                      AND d.permission_id = %s
                      AND d.user_id = %s
                      AND d.decision = %s
                      AND d.decision_timestamp = %s
                      AND d.payload_hash = %s
                      AND d.consumed_at IS NULL
                      AND d.expires_at > %s
                      AND EXISTS (
                          SELECT 1 FROM permissions AS p
                          WHERE p.permission_id::text = d.permission_id
                            AND p.status = 'REQUIRE_HUMAN'
                            AND p.mode = d.mode
                            AND p.expires_at > %s
                      )
                    RETURNING d.decision
                    """,
                    (current, decision_id, permission_id, _required_text(decision, "user_id"),
                     decision["decision"], metadata["decision_at"], canonical_hash(dict(decision)),
                     current, current),
                ).fetchone()
                if row is None:
                    return {"consumed": False, "status": "DENY",
                            "blockers": ["DECISION_UNREGISTERED_EXPIRED_MISMATCHED_OR_REPLAYED"],
                            "trade_allowed": False}
                status_value = "CONSUMED_APPROVE" if row[0] == "APPROVE" else "CONSUMED_REJECT"
                blockers = [] if row[0] == "APPROVE" else ["USER_REJECTED"]
                return {"consumed": True, "status": status_value, "decision_id": decision_id,
                        "blockers": blockers, "trade_allowed": False}
        except Exception as exc:
            raise DecisionStoreError("authorization storage is unavailable") from exc
