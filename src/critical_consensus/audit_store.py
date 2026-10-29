"""Transactional append-only PostgreSQL persistence for hash-linked audit traces.

This adapter persists and verifies traces, but never grants execution permission.
Callers should treat storage failures as a denial and must not dispatch an order.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Callable, Mapping, Sequence
import uuid

from psycopg.types.json import Jsonb

from .audit import canonical_hash, verify_audit_chain


MAX_TRACE_EVENTS = 10_000

_CREATE_TRACES = """
CREATE TABLE IF NOT EXISTS audit_traces (
    trace_id UUID PRIMARY KEY,
    signal_id TEXT NOT NULL,
    pipeline_status TEXT NOT NULL,
    event_count INTEGER NOT NULL CHECK (event_count > 0),
    chain_head TEXT NOT NULL,
    events_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
_CREATE_TRACE_EVENTS = """
CREATE TABLE IF NOT EXISTS audit_trace_events (
    trace_id UUID NOT NULL REFERENCES audit_traces(trace_id) ON DELETE RESTRICT,
    event_index INTEGER NOT NULL CHECK (event_index >= 0),
    event_type TEXT NOT NULL,
    timestamp_text TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    previous_hash TEXT,
    event_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (trace_id, event_index),
    UNIQUE (trace_id, event_hash)
)
"""
_CREATE_TRACE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_audit_trace_events_hash
ON audit_trace_events (event_hash)
"""
_CREATE_IMMUTABILITY_FUNCTION = """
CREATE OR REPLACE FUNCTION reject_audit_trace_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit trace storage is append-only; UPDATE/DELETE is prohibited';
END;
$$
"""
_CREATE_TRACE_TRIGGER = """
CREATE TRIGGER audit_traces_no_update_delete
BEFORE UPDATE OR DELETE ON audit_traces
FOR EACH ROW EXECUTE FUNCTION reject_audit_trace_mutation()
"""
_CREATE_EVENT_TRIGGER = """
CREATE TRIGGER audit_trace_events_no_update_delete
BEFORE UPDATE OR DELETE ON audit_trace_events
FOR EACH ROW EXECUTE FUNCTION reject_audit_trace_mutation()
"""


class AuditStoreError(RuntimeError):
    """The durable audit store failed; callers must fail closed."""


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _normalise_trace(events: Any) -> tuple[list[dict[str, Any]] | None, list[str]]:
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)) or not events:
        return None, ["AUDIT_TRACE_EMPTY_OR_INVALID"]
    if len(events) > MAX_TRACE_EVENTS:
        return None, ["AUDIT_TRACE_TOO_LARGE"]

    trace: list[dict[str, Any]] = []
    allowed_fields = {"event_type", "timestamp", "previous_hash", "payload", "event_hash"}
    for event in events:
        if not isinstance(event, Mapping):
            return None, ["AUDIT_TRACE_EVENT_INVALID"]
        row = dict(event)
        if set(row) != allowed_fields:
            return None, ["AUDIT_TRACE_EVENT_FIELDS_INVALID"]
        event_type = row.get("event_type")
        if not isinstance(event_type, str) or not event_type.strip():
            return None, ["AUDIT_TRACE_EVENT_TYPE_INVALID"]
        if _parse_timestamp(row.get("timestamp")) is None:
            return None, ["AUDIT_TRACE_TIMESTAMP_INVALID"]
        if not isinstance(row.get("payload"), Mapping):
            return None, ["AUDIT_TRACE_PAYLOAD_INVALID"]
        trace.append(row)

    chain = verify_audit_chain(trace)
    if chain.get("valid") is not True:
        return None, ["AUDIT_TRACE_CHAIN_INVALID"]
    signal_ids = []
    for event in trace:
        value = event["payload"].get("signal_id")
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            return None, ["AUDIT_TRACE_SIGNAL_ID_INVALID"]
        signal_ids.append(value.strip())
    if len(set(signal_ids)) > 1:
        return None, ["AUDIT_TRACE_SIGNAL_ID_MISMATCH"]
    return trace, []


def _json_safe(value: Mapping[str, Any]) -> dict[str, Any]:
    # Match audit.canonical_hash JSON semantics for values such as datetime instances.
    return json.loads(json.dumps(dict(value), sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False, default=str))


def _reconstruct_trace(
    header: tuple[Any, ...] | None,
    rows: Sequence[tuple[Any, ...]],
) -> dict[str, Any]:
    if header is None:
        return {"valid": False, "blockers": ["AUDIT_TRACE_NOT_FOUND"], "trade_allowed": False}

    trace_id, signal_id, pipeline_status, event_count, chain_head, events_hash = header
    events: list[dict[str, Any]] = []
    for expected_index, row in enumerate(rows):
        event_index, event_type, timestamp_text, occurred_at, previous_hash, event_hash, payload = row
        if event_index != expected_index:
            return {"valid": False, "blockers": ["AUDIT_TRACE_EVENT_ORDER_INVALID"], "trade_allowed": False}
        parsed = _parse_timestamp(timestamp_text)
        if (parsed is None or not isinstance(occurred_at, datetime)
                or occurred_at.tzinfo is None or occurred_at.utcoffset() is None
                or parsed != occurred_at.astimezone(timezone.utc)):
            return {"valid": False, "blockers": ["AUDIT_TRACE_TIMESTAMP_STORAGE_MISMATCH"], "trade_allowed": False}
        if not isinstance(payload, Mapping):
            return {"valid": False, "blockers": ["AUDIT_TRACE_PAYLOAD_STORAGE_INVALID"], "trade_allowed": False}
        events.append({
            "event_type": event_type,
            "timestamp": timestamp_text,
            "previous_hash": previous_hash,
            "payload": dict(payload),
            "event_hash": event_hash,
        })

    chain = verify_audit_chain(events)
    if chain.get("valid") is not True:
        return {"valid": False, "blockers": ["PERSISTED_AUDIT_CHAIN_INVALID"], "chain_check": chain,
                "trade_allowed": False}
    if len(events) != event_count:
        return {"valid": False, "blockers": ["AUDIT_TRACE_EVENT_COUNT_MISMATCH"], "trade_allowed": False}
    if not events or events[-1]["event_hash"] != chain_head:
        return {"valid": False, "blockers": ["AUDIT_TRACE_HEAD_MISMATCH"], "trade_allowed": False}
    if canonical_hash({"events": events}) != events_hash:
        return {"valid": False, "blockers": ["AUDIT_TRACE_CONTENT_HASH_MISMATCH"], "trade_allowed": False}
    seen_signal_ids = {
        event["payload"].get("signal_id")
        for event in events
        if isinstance(event["payload"].get("signal_id"), str)
    }
    derived_signal_id = next(iter(seen_signal_ids), "unknown")
    last_status = events[-1]["payload"].get("status")
    derived_status = last_status if isinstance(last_status, str) and last_status else "UNKNOWN"
    if derived_signal_id != signal_id or derived_status != pipeline_status:
        return {"valid": False, "blockers": ["AUDIT_TRACE_HEADER_MISMATCH"], "trade_allowed": False}
    return {
        "valid": True, "trace_id": str(trace_id), "signal_id": signal_id,
        "pipeline_status": pipeline_status, "events_verified": len(events),
        "chain_head": chain_head, "events_hash": events_hash,
        "events": events, "chain_check": chain, "trade_allowed": False,
    }


class PostgresAuditStore:
    """Append-only, idempotent trace store using a psycopg-compatible connection factory."""

    def __init__(self, connection_factory: Callable[[], Any]):
        if not callable(connection_factory):
            raise ValueError("connection_factory is required")
        self._connect = connection_factory

    def ensure_schema(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute(_CREATE_TRACES)
                connection.execute(_CREATE_TRACE_EVENTS)
                connection.execute(_CREATE_TRACE_INDEX)
                connection.execute(_CREATE_IMMUTABILITY_FUNCTION)
                connection.execute("DROP TRIGGER IF EXISTS audit_traces_no_update_delete ON audit_traces")
                connection.execute(_CREATE_TRACE_TRIGGER)
                connection.execute("DROP TRIGGER IF EXISTS audit_trace_events_no_update_delete ON audit_trace_events")
                connection.execute(_CREATE_EVENT_TRIGGER)
                connection.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_traces FROM PUBLIC")
                connection.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_trace_events FROM PUBLIC")
        except Exception as exc:
            raise AuditStoreError("audit storage schema is unavailable") from exc

    def persist_trace(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        trace_id: str | uuid.UUID | None = None,
    ) -> dict[str, Any]:
        trace, blockers = _normalise_trace(events)
        if blockers or trace is None:
            return {"persisted": False, "verified": False, "blockers": blockers or ["AUDIT_TRACE_INVALID"],
                    "trade_allowed": False}

        events_hash = canonical_hash({"events": trace})
        try:
            chosen_id = uuid.uuid5(uuid.NAMESPACE_URL, events_hash) if trace_id is None else uuid.UUID(str(trace_id))
        except (ValueError, TypeError, AttributeError):
            return {"persisted": False, "verified": False, "blockers": ["AUDIT_TRACE_ID_INVALID"],
                    "trade_allowed": False}

        signal_ids = [
            event["payload"].get("signal_id") for event in trace
            if isinstance(event["payload"].get("signal_id"), str)
        ]
        signal_id = signal_ids[0] if signal_ids else "unknown"
        last_status = trace[-1]["payload"].get("status")
        pipeline_status = last_status if isinstance(last_status, str) and last_status else "UNKNOWN"
        chain_head = trace[-1]["event_hash"]

        try:
            with self._connect() as connection:
                inserted = connection.execute(
                    """
                    INSERT INTO audit_traces
                        (trace_id, signal_id, pipeline_status, event_count, chain_head, events_hash)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (trace_id) DO NOTHING
                    RETURNING trace_id
                    """,
                    (chosen_id, signal_id, pipeline_status, len(trace), chain_head, events_hash),
                ).fetchone()

                if inserted is None:
                    header = connection.execute(
                        """
                        SELECT trace_id, signal_id, pipeline_status, event_count, chain_head, events_hash
                        FROM audit_traces WHERE trace_id = %s FOR UPDATE
                        """,
                        (chosen_id,),
                    ).fetchone()
                    if header is None:
                        raise AuditStoreError("audit trace identity disappeared during insert")
                    if (header[1] != signal_id or header[2] != pipeline_status
                            or header[3] != len(trace) or header[4] != chain_head or header[5] != events_hash):
                        return {"persisted": False, "verified": False, "trace_id": str(chosen_id),
                                "blockers": ["AUDIT_TRACE_ID_COLLISION"], "trade_allowed": False}
                    stored = self._verify_trace_in_connection(connection, chosen_id)
                    if stored.get("valid") is not True:
                        return {"persisted": False, "verified": False, "trace_id": str(chosen_id),
                                "blockers": ["PERSISTED_AUDIT_TRACE_INTEGRITY_FAILED"],
                                "verification": stored, "trade_allowed": False}
                    return {"persisted": True, "verified": True, "already_present": True,
                            "trace_id": str(chosen_id), "event_count": len(trace),
                            "chain_head": chain_head, "trade_allowed": False}

                for index, event in enumerate(trace):
                    parsed_time = _parse_timestamp(event["timestamp"])
                    if parsed_time is None:
                        raise AuditStoreError("audit event timestamp became invalid")
                    payload = _json_safe(event["payload"])
                    connection.execute(
                        """
                        INSERT INTO audit_trace_events
                            (trace_id, event_index, event_type, timestamp_text, occurred_at,
                             previous_hash, event_hash, payload)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (chosen_id, index, event["event_type"], event["timestamp"], parsed_time,
                         event["previous_hash"], event["event_hash"], Jsonb(payload)),
                    )
                stored = self._verify_trace_in_connection(connection, chosen_id)
                if stored.get("valid") is not True:
                    raise AuditStoreError("audit trace failed verification after persistence")
                return {"persisted": True, "verified": True, "already_present": False,
                        "trace_id": str(chosen_id), "event_count": len(trace),
                        "chain_head": chain_head, "trade_allowed": False}
        except AuditStoreError:
            raise
        except Exception as exc:
            raise AuditStoreError("audit trace could not be durably persisted") from exc

    def _verify_trace_in_connection(self, connection: Any, trace_id: uuid.UUID) -> dict[str, Any]:
        header = connection.execute(
            """
            SELECT trace_id, signal_id, pipeline_status, event_count, chain_head, events_hash
            FROM audit_traces WHERE trace_id = %s
            """,
            (trace_id,),
        ).fetchone()
        if header is None:
            return {"valid": False, "blockers": ["AUDIT_TRACE_NOT_FOUND"], "trade_allowed": False}
        rows = connection.execute(
            """
            SELECT event_index, event_type, timestamp_text, occurred_at, previous_hash, event_hash, payload
            FROM audit_trace_events WHERE trace_id = %s ORDER BY event_index
            """,
            (trace_id,),
        ).fetchall()
        return _reconstruct_trace(header, rows)

    def verify_trace(self, trace_id: str | uuid.UUID) -> dict[str, Any]:
        try:
            chosen_id = trace_id if isinstance(trace_id, uuid.UUID) else uuid.UUID(str(trace_id))
        except (ValueError, TypeError, AttributeError):
            return {"valid": False, "blockers": ["AUDIT_TRACE_ID_INVALID"], "trade_allowed": False}
        try:
            with self._connect() as connection:
                return self._verify_trace_in_connection(connection, chosen_id)
        except Exception as exc:
            raise AuditStoreError("audit trace verification failed") from exc
