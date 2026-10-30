"""Append-only audit helpers; production storage must enforce immutability at DB/object-store level."""
from __future__ import annotations
import hashlib, json
from datetime import datetime, timezone
from typing import Any, Mapping


def canonical_hash(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def make_audit_event(event_type: str, payload: Mapping[str, Any], previous_hash: str | None = None) -> dict[str, Any]:
    body = {"event_type": event_type, "timestamp": datetime.now(timezone.utc).isoformat(),
            "previous_hash": previous_hash, "payload": dict(payload)}
    body["event_hash"] = canonical_hash(body)
    return body


def verify_audit_chain(events: list[Mapping[str, Any]], *, initial_previous_hash: str | None = None) -> dict[str, Any]:
    """Verify event hashes and previous-hash links for an ordered audit export."""
    expected_previous = initial_previous_hash
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            return {"valid": False, "index": index, "reason": "INVALID_EVENT"}
        if event.get("previous_hash") != expected_previous:
            return {"valid": False, "index": index, "reason": "PREVIOUS_HASH_MISMATCH"}
        stored_hash = event.get("event_hash")
        if not isinstance(stored_hash, str) or not stored_hash.startswith("sha256:"):
            return {"valid": False, "index": index, "reason": "EVENT_HASH_MISSING"}
        body = {key: value for key, value in event.items() if key != "event_hash"}
        if canonical_hash(body) != stored_hash:
            return {"valid": False, "index": index, "reason": "EVENT_CONTENT_HASH_MISMATCH"}
        expected_previous = stored_hash
    return {"valid": True, "events_verified": len(events), "last_hash": expected_previous}
