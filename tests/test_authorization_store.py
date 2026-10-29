from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import uuid

import psycopg
from psycopg import sql
import pytest

from critical_consensus.audit import canonical_hash
from critical_consensus.authorization_store import (
    PostgresDecisionStore,
    VerifiedAuthReceipt,
)

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.fixture
def store():
    assert DATABASE_URL is not None
    schema_name = f"test_cc_{uuid.uuid4().hex}"

    with psycopg.connect(DATABASE_URL) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))

    def connect():
        connection = psycopg.connect(DATABASE_URL)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
        return connection

    with connect() as connection:
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
    with psycopg.connect(DATABASE_URL) as admin:
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name)))


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


def make_receipt(now, decision, *, two_fa=False, signature=False):
    return VerifiedAuthReceipt(
        verifier_id="test-auth-provider",
        method="webauthn",
        receipt_id=f"receipt-{uuid.uuid4().hex}",
        verified_at=now.isoformat(),
        decision_hash=canonical_hash(dict(decision)),
        two_fa_verified=two_fa,
        signature_verified=signature,
    )


def test_registered_decision_can_be_consumed_once_only(store):
    permission_id, now = seed_permission(store)
    decision = make_decision(permission_id, now)
    registration = store.register_verified_decision(decision, make_receipt(now, decision), now=now)
    assert registration["registered"] is True

    permission = {"status": "REQUIRE_HUMAN", "permission_id": permission_id}
    first = store.consume_decision(permission, decision, now=now + timedelta(seconds=1))
    second = store.consume_decision(permission, decision, now=now + timedelta(seconds=2))
    assert first["consumed"] is True
    assert first["status"] == "CONSUMED_APPROVE"
    assert first["trade_allowed"] is False
    assert second["consumed"] is False
    assert second["status"] == "DENY"
    assert "PERMISSION_NOT_PENDING_HUMAN_APPROVAL" in second["blockers"][0]


def test_mismatched_payload_cannot_consume_registered_decision(store):
    permission_id, now = seed_permission(store)
    decision = make_decision(permission_id, now)
    assert store.register_verified_decision(decision, make_receipt(now, decision), now=now)["registered"] is True
    permission = {"status": "REQUIRE_HUMAN", "permission_id": permission_id}

    forged = dict(decision)
    forged["reason"] = "Different decision payload"
    assert store.consume_decision(permission, forged, now=now + timedelta(seconds=1))["consumed"] is False
    assert store.consume_decision(permission, decision, now=now + timedelta(seconds=1))["consumed"] is True


def test_store_refuses_expired_permission_and_duplicate_decision_id(store):
    permission_id, now = seed_permission(store, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    decision = make_decision(permission_id, now)
    result = store.register_verified_decision(decision, make_receipt(now, decision), now=now)
    assert result["registered"] is False
    assert "EXPIRED" in result["blockers"][0]

    permission_id, now = seed_permission(store)
    decision = make_decision(permission_id, now)
    receipt = make_receipt(now, decision)
    assert store.register_verified_decision(decision, receipt, now=now)["registered"] is True
    duplicate_id = store.register_verified_decision(decision, receipt, now=now)
    assert duplicate_id["registered"] is False
    assert "ALREADY_REGISTERED" in duplicate_id["blockers"][0]

    second_decision = make_decision(permission_id, now)
    duplicate_permission = store.register_verified_decision(second_decision, make_receipt(now, second_decision), now=now)
    assert duplicate_permission["registered"] is False
    assert "ALREADY_REGISTERED" in duplicate_permission["blockers"][0]


def test_live_approval_requires_verified_second_factor_and_signature(store):
    permission_id, now = seed_permission(store, mode="live")
    decision = make_decision(permission_id, now, two_fa_verified=True, signature_verified=True)
    no_proof = make_receipt(now, decision, two_fa=False, signature=False)
    result = store.register_verified_decision(decision, no_proof, now=now)
    assert result["registered"] is False
    assert "LIVE_2FA_OR_SIGNATURE_VERIFICATION_REQUIRED" in result["blockers"][0]

    valid_proof = make_receipt(now, decision, two_fa=True, signature=True)
    result = store.register_verified_decision(decision, valid_proof, now=now)
    assert result["registered"] is True


def test_rejected_human_decision_is_consumed_but_never_authorizes(store):
    permission_id, now = seed_permission(store)
    decision = make_decision(permission_id, now, decision="REJECT")
    assert store.register_verified_decision(decision, make_receipt(now, decision), now=now)["registered"] is True
    result = store.consume_decision({"status": "REQUIRE_HUMAN", "permission_id": permission_id},
                                    decision, now=now + timedelta(seconds=1))
    assert result["status"] == "CONSUMED_REJECT"
    assert result["trade_allowed"] is False
    assert result["blockers"] == ["USER_REJECTED"]
    with store._connect() as connection:
        state = connection.execute(
            "SELECT status FROM permissions WHERE permission_id::text = %s", (permission_id,)
        ).fetchone()
    assert state == ("DECISION_CONSUMED",)


def test_concurrent_consume_has_exactly_one_winner(store):
    permission_id, now = seed_permission(store)
    decision = make_decision(permission_id, now)
    assert store.register_verified_decision(decision, make_receipt(now, decision), now=now)["registered"] is True
    permission = {"status": "REQUIRE_HUMAN", "permission_id": permission_id}

    def consume():
        return store.consume_decision(permission, decision, now=now + timedelta(seconds=1))["consumed"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: consume(), range(8)))
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 7
    with store._connect() as connection:
        state = connection.execute(
            "SELECT status FROM permissions WHERE permission_id::text = %s", (permission_id,)
        ).fetchone()
    assert state == ("DECISION_CONSUMED",)


def test_store_fails_closed_for_unverified_receipt_and_ttl_outside_cap(store):
    permission_id, now = seed_permission(store)
    decision = make_decision(permission_id, now, ttl_seconds=301)
    result = store.register_verified_decision(decision, make_receipt(now, decision), now=now)
    assert result["registered"] is False
    assert "TTL_INVALID" in result["blockers"][0]

    decision = make_decision(permission_id, now)
    result = store.register_verified_decision(decision, {"verified": True}, now=now)
    assert result["registered"] is False
    assert "RECEIPT_REQUIRED" in result["blockers"][0]


def test_consumed_approval_closes_permission_without_granting_trade(store):
    permission_id, now = seed_permission(store)
    decision = make_decision(permission_id, now)
    assert store.register_verified_decision(decision, make_receipt(now, decision), now=now)["registered"] is True

    result = store.consume_decision(
        {"status": "REQUIRE_HUMAN", "permission_id": permission_id}, decision,
        now=now + timedelta(seconds=1),
    )
    assert result["status"] == "CONSUMED_APPROVE"
    assert result["trade_allowed"] is False
    with store._connect() as connection:
        state = connection.execute(
            "SELECT status FROM permissions WHERE permission_id::text = %s", (permission_id,)
        ).fetchone()
    assert state == ("DECISION_CONSUMED",)


def test_revoked_permission_cannot_consume_registered_decision(store):
    permission_id, now = seed_permission(store)
    decision = make_decision(permission_id, now)
    assert store.register_verified_decision(decision, make_receipt(now, decision), now=now)["registered"] is True
    with store._connect() as connection:
        connection.execute("UPDATE permissions SET status = 'DENY' WHERE permission_id::text = %s",
                           (permission_id,))

    result = store.consume_decision(
        {"status": "REQUIRE_HUMAN", "permission_id": permission_id}, decision,
        now=now + timedelta(seconds=1),
    )
    assert result["consumed"] is False
    assert result["status"] == "DENY"
    assert "PERMISSION_NOT_PENDING_HUMAN_APPROVAL" in result["blockers"]


def test_verified_receipt_is_bound_to_exact_decision_payload(store):
    permission_id, now = seed_permission(store)
    decision = make_decision(permission_id, now)
    receipt = make_receipt(now, decision)
    altered = dict(decision)
    altered["reason"] = "tampered after verification"

    rejected = store.register_verified_decision(altered, receipt, now=now)
    assert rejected["registered"] is False
    assert "VERIFIED_AUTH_DECISION_BINDING_MISMATCH" in rejected["blockers"]

    accepted = store.register_verified_decision(decision, receipt, now=now)
    assert accepted["registered"] is True


def test_existing_permission_status_constraint_migrates_for_consumed_state(store):
    permission_id, _now = seed_permission(store)
    with store._connect() as connection:
        connection.execute(
            "ALTER TABLE permissions ADD CONSTRAINT permissions_status_check "
            "CHECK (status IN ('DENY','REQUIRE_HUMAN','ALLOW_ONCE','EXPIRED'))"
        )

    migration_path = Path(__file__).resolve().parents[1] / "db" / "migrations" / "20261029_permission_decision_consumed.sql"
    migration_sql = "\n".join(
        line for line in migration_path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    )
    statements = migration_sql.split(";")
    with store._connect() as connection:
        for statement in statements:
            if statement.strip():
                connection.execute(statement)
        connection.execute(
            "UPDATE permissions SET status = 'DECISION_CONSUMED' WHERE permission_id::text = %s",
            (permission_id,),
        )
        state = connection.execute(
            "SELECT status FROM permissions WHERE permission_id::text = %s", (permission_id,)
        ).fetchone()
    assert state == ("DECISION_CONSUMED",)
