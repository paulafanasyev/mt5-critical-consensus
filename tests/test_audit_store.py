from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
import pytest

from critical_consensus.audit import make_audit_event
from critical_consensus.audit_store import AuditStoreError, PostgresAuditStore

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.fixture
def store():
    assert DATABASE_URL is not None
    schema_name = f"test_audit_{uuid.uuid4().hex}"

    with psycopg.connect(DATABASE_URL) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))

    def connect():
        connection = psycopg.connect(DATABASE_URL)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
        return connection

    result = PostgresAuditStore(connect)
    result.ensure_schema()
    yield result
    with psycopg.connect(DATABASE_URL) as admin:
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name)))


def make_trace(signal_id="signal-a", status="GATHER_MORE"):
    first = make_audit_event("SIGNAL_RECEIVED", {"signal_id": signal_id, "source": "unit-test"})
    last = make_audit_event("PIPELINE_RESULT", {"signal_id": signal_id, "status": status}, first["event_hash"])
    return [first, last]


def test_persisted_trace_round_trips_and_verifies(store):
    events = make_trace()
    result = store.persist_trace(events)
    assert result["persisted"] is True
    assert result["verified"] is True
    assert result["already_present"] is False
    assert result["event_count"] == 2
    assert result["trade_allowed"] is False

    verified = store.verify_trace(result["trace_id"])
    assert verified["valid"] is True
    assert verified["events_verified"] == 2
    assert verified["events"] == events
    assert verified["chain_head"] == events[-1]["event_hash"]


def test_persistence_rejects_tampered_chain_before_write(store):
    events = make_trace()
    events[0]["payload"]["source"] = "tampered"
    result = store.persist_trace(events)
    assert result["persisted"] is False
    assert result["verified"] is False
    assert "AUDIT_TRACE_CHAIN_INVALID" in result["blockers"]
    assert store.verify_trace(result.get("trace_id", str(uuid.uuid4())))["valid"] is False


def test_same_trace_is_idempotent_and_does_not_duplicate_rows(store):
    events = make_trace()
    first = store.persist_trace(events)
    second = store.persist_trace(events)
    assert first["trace_id"] == second["trace_id"]
    assert first["already_present"] is False
    assert second["already_present"] is True
    with store._connect() as connection:
        count = connection.execute(
            "SELECT count(*) FROM audit_trace_events WHERE trace_id = %s",
            (uuid.UUID(first["trace_id"]),),
        ).fetchone()[0]
    assert count == len(events)


def test_trace_id_collision_with_different_contents_fails_closed(store):
    first_events = make_trace("signal-a")
    trace_id = uuid.uuid4()
    first = store.persist_trace(first_events, trace_id=trace_id)
    assert first["persisted"] is True
    different_events = make_trace("signal-b")
    second = store.persist_trace(different_events, trace_id=trace_id)
    assert second["persisted"] is False
    assert second["verified"] is False
    assert "AUDIT_TRACE_ID_COLLISION" in second["blockers"]


def test_concurrent_same_trace_persists_only_one_copy(store):
    events = make_trace()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.persist_trace(events), range(8)))
    assert all(result["persisted"] and result["verified"] for result in results)
    assert sum(not result["already_present"] for result in results) == 1
    trace_id = results[0]["trace_id"]
    with store._connect() as connection:
        count = connection.execute(
            "SELECT count(*) FROM audit_trace_events WHERE trace_id = %s",
            (uuid.UUID(trace_id),),
        ).fetchone()[0]
    assert count == len(events)


def test_append_only_triggers_reject_update_and_delete(store):
    result = store.persist_trace(make_trace())
    trace_id = uuid.UUID(result["trace_id"])
    with pytest.raises(psycopg.Error):
        with store._connect() as connection:
            connection.execute("UPDATE audit_traces SET pipeline_status = 'PASS' WHERE trace_id = %s",
                               (trace_id,))
    with pytest.raises(psycopg.Error):
        with store._connect() as connection:
            connection.execute("DELETE FROM audit_trace_events WHERE trace_id = %s", (trace_id,))


def test_verify_trace_rejects_invalid_id(store):
    result = store.verify_trace("not-a-uuid")
    assert result["valid"] is False
    assert "AUDIT_TRACE_ID_INVALID" in result["blockers"]
