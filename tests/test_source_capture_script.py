from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "capture_source_evidence", ROOT / "scripts" / "capture_source_evidence.py",
)
CAPTURE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CAPTURE)


def policy():
    return {
        "version": "1", "max_clock_skew_seconds": 60, "max_capture_age_seconds": 300,
        "max_source_age_seconds": 900, "max_content_bytes": 512000,
        "min_independent_publishers": 2,
        "publishers": {
            "official-a": {
                "domains": ["official-a.example"], "independence_group": "publisher-a",
                "credibility": 0.95, "ttl_seconds": 900,
                "allowed_publication_time_attestations": ["operator_attested"],
                "allowed_query_parameters": [],
            },
        },
    }


def test_capture_manifest_adds_content_type_before_provenance_validation(tmp_path, monkeypatch):
    body = b'{"headline":"Central bank statement","body":"Policy rate remains unchanged"}'
    monkeypatch.setattr(CAPTURE, "_fetch", lambda url, max_bytes, timeout: (body, "application/json"))
    registry_path = tmp_path / "registry.yaml"
    manifest_path = tmp_path / "manifest.json"
    registry_path.write_text(yaml.safe_dump(policy()), encoding="utf-8")
    manifest_path.write_text(json.dumps({
        "sources": [{
            "publisher_id": "official-a",
            "url": "https://official-a.example/releases/rate-decision",
            "published_at": "2026-10-30T01:58:00Z",
            "publication_time_attestation": "operator_attested",
        }],
    }), encoding="utf-8")
    records = CAPTURE.capture_manifest(manifest_path, registry_path)
    assert len(records) == 1
    assert records[0]["response_content_type"] == "application/json"
    assert records[0]["captured_content"] == body
    assert records[0]["content_bytes"] == len(body)
    assert records[0]["source_id"].startswith("src-")


def test_capture_manifest_rejects_unapproved_query_and_non_operator_date_basis(tmp_path, monkeypatch):
    body = b'{"headline":"Official statement","body":"Details from publisher"}'
    monkeypatch.setattr(CAPTURE, "_fetch", lambda url, max_bytes, timeout: (body, "application/json"))
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(yaml.safe_dump(policy()), encoding="utf-8")

    manifest_path = tmp_path / "manifest.json"
    base = {
        "publisher_id": "official-a",
        "url": "https://official-a.example/releases/rate-decision?token=abc",
        "published_at": "2026-10-30T01:58:00Z",
        "publication_time_attestation": "operator_attested",
    }
    manifest_path.write_text(json.dumps({"sources": [base]}), encoding="utf-8")
    try:
        CAPTURE.capture_manifest(manifest_path, registry_path)
        assert False, "credential-like query parameter should have been rejected"
    except ValueError as exc:
        assert "credential-like" in str(exc) or "unapproved" in str(exc)

    base["url"] = "https://official-a.example/releases/rate-decision"
    base["publication_time_attestation"] = "publisher_api_field"
    registry = policy()
    registry["publishers"]["official-a"]["allowed_publication_time_attestations"].append("publisher_api_field")
    registry_path.write_text(yaml.safe_dump(registry), encoding="utf-8")
    manifest_path.write_text(json.dumps({"sources": [base]}), encoding="utf-8")
    try:
        CAPTURE.capture_manifest(manifest_path, registry_path)
        assert False, "generic fetcher must not claim API-specific timestamp verification"
    except ValueError as exc:
        assert "publication-time attestation" in str(exc)
