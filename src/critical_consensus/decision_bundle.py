"""Build a provenance-bound handoff from agent review to deterministic gates.

This module copies only evidence already produced by the trusted analysis runner.
It deliberately does not invent Chief reasoning, calibration, permission requests,
human approval, or executable commands.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .audit import canonical_hash


def _records(value: Any, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{field} must be a sequence of records")
    if any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"{field} contains a non-object record")
    return [dict(item) for item in value]


def build_decision_bundle(
    signal: Mapping[str, Any],
    collection_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Create the exact handoff for evaluate_signal_bundle without filling missing gates."""
    if not isinstance(signal, Mapping) or not isinstance(collection_result, Mapping):
        raise ValueError("signal and collection_result must be objects")

    signal_id = str(signal.get("signal_id", "")).strip()
    if not signal_id or str(collection_result.get("signal_id", "")).strip() != signal_id:
        raise ValueError("signal_id is missing or collection result is bound to another signal")

    raw_refs = signal.get("available_evidence_refs")
    if (
        not isinstance(raw_refs, Sequence)
        or isinstance(raw_refs, (str, bytes, bytearray))
        or not raw_refs
        or any(not isinstance(ref, str) or not ref.strip() for ref in raw_refs)
    ):
        raise ValueError("signal evidence allowlist is missing or invalid")
    allowed_refs = {ref.strip() for ref in raw_refs}
    if len(allowed_refs) != len(raw_refs):
        raise ValueError("signal evidence allowlist contains duplicate references")

    signal_input = dict(signal)
    signal_input_hash = canonical_hash(signal_input)
    verdicts = _records(collection_result.get("verdicts", []), "verdicts")
    for index, verdict in enumerate(verdicts):
        if str(verdict.get("signal_id", "")).strip() != signal_id:
            raise ValueError(f"verdict {index} is bound to another signal")
        if verdict.get("input_hash") != signal_input_hash:
            raise ValueError(f"verdict {index} input_hash does not match the exact signal")
        refs = verdict.get("evidence_refs")
        if (
            not isinstance(refs, Sequence)
            or isinstance(refs, (str, bytes, bytearray))
            or not refs
            or any(not isinstance(ref, str) or ref.strip() not in allowed_refs for ref in refs)
        ):
            raise ValueError(f"verdict {index} contains evidence outside the signal allowlist")

    peer_reviews = _records(collection_result.get("peer_reviews", []), "peer_reviews")
    challenge_records = _records(collection_result.get("challenge_records", []), "challenge_records")
    open_challenges = _records(collection_result.get("open_challenges", []), "open_challenges")

    source_documents = signal.get("source_documents", [])
    if not isinstance(source_documents, Sequence) or isinstance(source_documents, (str, bytes, bytearray)):
        raise ValueError("source_documents must be a sequence")
    key_sources: list[dict[str, str]] = []
    seen_source_ids: set[str] = set()
    for index, document in enumerate(source_documents):
        if not isinstance(document, Mapping):
            raise ValueError(f"source document {index} is not an object")
        source_id = str(document.get("source_id", "")).strip()
        if not source_id or source_id not in allowed_refs or source_id in seen_source_ids:
            raise ValueError(f"source document {index} has an invalid or duplicate evidence ID")
        seen_source_ids.add(source_id)
        key_sources.append({"source_id": source_id})

    blockers = collection_result.get("blockers", [])
    if not isinstance(blockers, Sequence) or isinstance(blockers, (str, bytes, bytearray)):
        raise ValueError("collection blockers must be a sequence")
    risk_flags = [str(item) for item in blockers if isinstance(item, str) and item.strip()]

    return {
        "signal_id": signal_id,
        "signal_input": signal_input,
        "signal_input_hash": signal_input_hash,
        "verdicts": verdicts,
        "peer_reviews": peer_reviews,
        "challenge_records": challenge_records,
        "open_challenges": open_challenges,
        "challenge_resolution_status": str(
            collection_result.get("challenge_resolution_status", "MISSING")
        ).upper(),
        "key_sources": key_sources,
        "risk_flags": risk_flags,
        "trade_allowed": False,
    }


def write_decision_bundle(path: str | Path, bundle: Mapping[str, Any]) -> Path:
    """Atomically write a JSON evidence packet; never write a partially encoded bundle."""
    if not isinstance(bundle, Mapping):
        raise ValueError("bundle must be an object")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        dict(bundle), indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
    ) + "\n"
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
    return destination
