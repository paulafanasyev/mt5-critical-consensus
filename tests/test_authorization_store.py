from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import uuid

import psycopg
import pytest

from critical_consensus.authorization_store import (
    PostgresDecisionStore,
    VerifiedAuthReceipt,
)

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.fixture
def store():
    assert DATABASE_URL is not None

    def connect():
        return psycopg.connect(DATABASE_URL)

    with connect() as connection:
        connection.execute("DROP TABLE IF EXISTS user_decisions")
        connection.execute("DROP TABLE IF EXISTS permissions")
        connection.execute(
            """
            CREATE TABLE permissions (
                permission_id UUID PRIMARY KEY,
                status TEXT NOT NULL,
                mode TEXT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL
            )
            """
        )
    result = PostgresDecisionStore(connect)
    result.ensure_schema()
    yield result
    with connect() as connection:
        connection.execute("DROP TABLE IF EXISTS user_decisions")
        connection.execute("DROP TABLE IF EXISTS permissions")


def seed_permission(store, *, mode="demo", status="REQUIRE_HUMAN", expires_at=None):
    now = datetime.now(timezone.utc)
    permission_id = str(uuid.uuid4())
    expiry = expires_at or now + timedelta(minutes=10)
    # The test table uses UUID to match the production schema.
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO permissions (permission_id, status, mode, expires_at) VALUES (%s, %s, %s, %s)",
            (permission_id, status, mode, expiry),
        )
    return permission_id, now


def make_decision(permission_id, now, *, decision="APPROVE", ttl_seconds=120, **extras):
    payload = {
        "decision_id": f"decision-{uuid.uuid4().hex}",
        "permission_id": permission_id,
        "user_id": "user-verified-1",
        "decision": decision,
        "timestamp": now.isoformat(),
        "reason": "Reviewed the complete risk packet",
        "ttl_seconds": ttl_seconds,
    }
    payload.update(extras)
    return payload


def make_receipt(now, *, two_fa=False, signature=False):
    return VerifiedAuthReceipt(
        verifier_id="test-auth-provider",
        method="webauthn",
        receipt_id=f"receipt-{uuid.uuid4().hex}",
        verified_at=now.isoformat(),
        two_fa_verified=two_fa,
        signature_verified=signature,
    )


def test_registered_decision_can_be_consumed_once_only(store):
    permission_id, now = seed_permission(store)
    decision = make_decision(permission_id, now)
    registration = store.register_verified_decision(decision, make_receipt(now), now=now)
    assert registration["registered"] is True

    permission = {"status": "REQUIRE_HUMAN", "permission_id": permission_id}
    first = store.consume_decision(permission, decision, now=now + timedelta(seconds=1))
    second = store.consume_decision(permission, decision, now=now + timedelta(seconds=2))
    assert first["consumed"] is True
    assert first["status"] == "CONSUMED_APPROVE"
    assert first["trade_allowed"] is False
    assert second["consumed"] is False
    assert "REPLAYED" in second["blockers"][0]


def test_mismatched_payload_cannot_consume_registered_decision(store):
    permission_id, now = seed_permission(store)
    decision = make_decision(permission_id, now)
    assert store.register_verified_decision(decision, make_receipt(now), now=now)["registered"] is True
    permission = {"status": "REQUIRE_HUMAN", "permission_id": permission_id}

    forged = dict(decision)
    forged["reason"] = "Different decision payload"
    assert store.consume_decision(permission, forged, now=now + timedelta(seconds=1))["consumed"] is False
    assert store.consume_decision(permission, decision, now=now + timedelta(seconds=1))["consumed"] is True


def test_store_refuses_expired_permission_and_duplicate_decision_id(store):
    permission_id, now = seed_permission(store, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    decision = make_decision(permission_id, now)
    result = store.register_verified_decision(decision, make_receipt(now), now=now)
    assert result["registered"] is False
    assert "EXPIRED" in result["blockers"][0]

    permission_id, now = seed_permission(store)
    decision = make_decision(permission_id, now)
    receipt = make_receipt(now)
    assert store.register_verified_decision(decision, receipt, now=now)["registered"] is True
    duplicate = store.register_verified_decision(decision, receipt, now=now)
    assert duplicate["registered"] is False
    assert "ALREADY_REGISTERED" in duplicate["blockers"][0]


def test_live_approval_requires_verified_second_factor_and_signature(store):
    permission_id, now = seed_permission(store, mode="live")
    decision = make_decision(permission_id, now, two_fa_verified=True, signature_verified=True)
    no_proof = make_receipt(now, two_fa=False, signature=False)
    result = store.register_verified_decision(decision, no_proof, now=now)
    assert result["registered"] is False
    assert "LIVE_2FA_OR_SIGNATURE_VERIFICATION_REQUIRED" in result["blockers"][0]

    valid_proof = make_receipt(now, two_fa=True, signature=True)
    result = store.register_verified_decision(decision, valid_proof, now=now)
    assert result["registered"] is True


def test_rejected_human_decision_is_consumed_but_never_authorizes(store):
    permission_id, now = seed_permission(store)
    decision = make_decision(permission_id, now, decision="REJECT")
    assert store.register_verified_decision(decision, make_receipt(now), now=now)["registered"] is True
    result = store.consume_decision({"status": "REQUIRE_HUMAN", "permission_id": permission_id},
                                    decision, now=now + timedelta(seconds=1))
    assert result["status"] == "CONSUMED_REJECT"
    assert result["trade_allowed"] is False
    assert result["blockers"] == ["USER_REJECTED"]


def test_concurrent_consume_has_exactly_one_winner(store):
    permission_id, now = seed_permission(store)
    decision = make_decision(permission_id, now)
    assert store.register_verified_decision(decision, make_receipt(now), now=now)["registered"] is True
    permission = {"status": "REQUIRE_HUMAN", "permission_id": permission_id}

    def consume():
        return store.consume_decision(permission, decision, now=now + timedelta(seconds=1))["consumed"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: consume(), range(8)))
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 7


def test_store_fails_closed_for_unverified_receipt_and_ttl_outside_cap(store):
    permission_id, now = seed_permission(store)
    decision = make_decision(permission_id, now, ttl_seconds=301)
    result = store.register_verified_decision(decision, make_receipt(now), now=now)
    assert result["registered"] is False
    assert "TTL_INVALID" in result["blockers"][0]

    decision = make_decision(permission_id, now)
    result = store.register_verified_decision(decision, {"verified": True}, now=now)
    assert result["registered"] is False
    assert "RECEIPT_REQUIRED" in result["blockers"][0]
