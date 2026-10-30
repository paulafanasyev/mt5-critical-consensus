from __future__ import annotations

import importlib.util
import socket
import json
from datetime import datetime, timedelta, timezone
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
            "published_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
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
        "published_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
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


def test_dns_resolution_rejects_private_and_loopback_addresses(monkeypatch):
    private_answer = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
    monkeypatch.setattr(CAPTURE.socket, "getaddrinfo", lambda *args, **kwargs: [private_answer])
    try:
        CAPTURE._resolve_public_addresses("official-a.example")
        assert False, "loopback DNS answer must be rejected"
    except ValueError as exc:
        assert "non-public" in str(exc)


def test_dns_resolution_returns_only_public_validated_addresses(monkeypatch):
    public_answer = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    monkeypatch.setattr(CAPTURE.socket, "getaddrinfo", lambda *args, **kwargs: [public_answer])
    assert CAPTURE._resolve_public_addresses("official-a.example") == ["93.184.216.34"]


def test_fetch_uses_the_prevalidated_ip_and_does_not_follow_redirects(monkeypatch):
    calls = {}
    class FakeResponse:
        status = 200
        def getheader(self, name, default=None):
            return {"Content-Type": "application/json", "Content-Length": "32"}.get(name, default)
        def read(self, size):
            return b'{"headline":"approved content"}'
        def close(self):
            calls["response_closed"] = True

    class FakeConnection:
        def __init__(self, hostname, pinned_ip, *, timeout):
            calls.update({"hostname": hostname, "pinned_ip": pinned_ip, "timeout": timeout})
        def request(self, method, path, headers):
            calls.update({"method": method, "path": path, "headers": headers})
        def getresponse(self):
            return FakeResponse()
        def close(self):
            calls["connection_closed"] = True

    monkeypatch.setattr(CAPTURE, "_resolve_public_addresses", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(CAPTURE, "_PinnedHTTPSConnection", FakeConnection)
    body, content_type = CAPTURE._fetch(
        "https://official-a.example/releases/item?id=42", max_bytes=512000, timeout=4,
    )
    assert calls["hostname"] == "official-a.example"
    assert calls["pinned_ip"] == "93.184.216.34"
    assert calls["method"] == "GET"
    assert calls["path"] == "/releases/item?id=42"
    assert calls["headers"]["Connection"] == "close"
    assert calls["response_closed"] is True
    assert calls["connection_closed"] is True
    assert body == b'{"headline":"approved content"}'
    assert content_type == "application/json"


def test_dns_resolution_rejects_mixed_public_and_private_answers(monkeypatch):
    public_answer = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    metadata_answer = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))
    monkeypatch.setattr(
        CAPTURE.socket, "getaddrinfo",
        lambda *args, **kwargs: [public_answer, metadata_answer],
    )
    try:
        CAPTURE._resolve_public_addresses("official-a.example")
        assert False, "mixed public/private DNS answers must be rejected"
    except ValueError as exc:
        assert "non-public" in str(exc)


def test_fetch_rejects_redirect_without_following_location(monkeypatch):
    calls = {"closed": False}
    class RedirectResponse:
        status = 302
        def close(self):
            calls["closed"] = True

    class FakeConnection:
        def __init__(self, hostname, pinned_ip, *, timeout):
            calls["hostname"] = hostname
        def request(self, method, path, headers):
            calls["method"] = method
        def getresponse(self):
            return RedirectResponse()
        def close(self):
            calls["connection_closed"] = True

    monkeypatch.setattr(CAPTURE, "_resolve_public_addresses", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(CAPTURE, "_PinnedHTTPSConnection", FakeConnection)
    try:
        CAPTURE._fetch("https://official-a.example/releases/item", max_bytes=512000, timeout=3)
        assert False, "redirect must be rejected"
    except RuntimeError as exc:
        assert str(exc) == "publisher_http_error:302"
    assert calls["closed"] is True
    assert calls["connection_closed"] is True
    assert calls["method"] == "GET"


def test_fetch_rejects_unbounded_or_invalid_response_size_policy():
    for invalid in (0, -1, 512001, True, 1.5):
        try:
            CAPTURE._fetch(
                "https://official-a.example/releases/item",
                max_bytes=invalid, timeout=3,
            )
            assert False, "invalid max_bytes must be rejected before DNS or network calls"
        except ValueError as exc:
            assert "max_bytes" in str(exc)


def test_capture_manifest_rejects_registry_size_limit_above_database_cap(tmp_path):
    registry = policy()
    registry["max_content_bytes"] = 512001
    registry_path = tmp_path / "registry.yaml"
    manifest_path = tmp_path / "manifest.json"
    registry_path.write_text(yaml.safe_dump(registry), encoding="utf-8")
    manifest_path.write_text(json.dumps({"sources": [{
        "publisher_id": "official-a",
        "url": "https://official-a.example/releases/rate-decision",
        "published_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        "publication_time_attestation": "operator_attested",
    }]}), encoding="utf-8")
    try:
        CAPTURE.capture_manifest(manifest_path, registry_path)
        assert False, "oversized registry cap should be rejected before any fetch"
    except ValueError as exc:
        assert "max_content_bytes" in str(exc)


def test_capture_manifest_extracts_publication_timestamp_from_configured_json_pointer(tmp_path, monkeypatch):
    published = datetime.now(timezone.utc) - timedelta(minutes=1)
    body = json.dumps({
        "metadata": {"publishedAt": published.isoformat()},
        "headline": "Official policy update",
        "body": "The publisher exposes an explicit publication timestamp field.",
    }).encode("utf-8")
    monkeypatch.setattr(CAPTURE, "_fetch", lambda url, max_bytes, timeout: (body, "application/json"))
    configured = policy()
    profile = configured["publishers"]["official-a"]
    profile["published_at_json_pointer"] = "/metadata/publishedAt"
    profile["allowed_publication_time_attestations"] = ["publisher_json_pointer"]
    registry_path = tmp_path / "registry.yaml"
    manifest_path = tmp_path / "manifest.json"
    registry_path.write_text(yaml.safe_dump(configured), encoding="utf-8")
    manifest_path.write_text(json.dumps({"sources": [{
        "publisher_id": "official-a",
        "url": "https://official-a.example/api/releases/latest",
    }]}), encoding="utf-8")
    records = CAPTURE.capture_manifest(manifest_path, registry_path)
    assert len(records) == 1
    record = records[0]
    assert record["published_at"] == published.isoformat()
    assert record["publication_time_attestation"] == "publisher_json_pointer"
    assert record["publication_time_field"] == "/metadata/publishedAt"
    assert record["source_id"].startswith("src-")


def test_capture_manifest_rejects_api_timestamp_pointer_when_field_missing(tmp_path, monkeypatch):
    body = b'{"metadata":{"published":"not-the-configured-field"}}'
    monkeypatch.setattr(CAPTURE, "_fetch", lambda url, max_bytes, timeout: (body, "application/json"))
    configured = policy()
    profile = configured["publishers"]["official-a"]
    profile["published_at_json_pointer"] = "/metadata/publishedAt"
    profile["allowed_publication_time_attestations"] = ["publisher_json_pointer"]
    registry_path = tmp_path / "registry.yaml"
    manifest_path = tmp_path / "manifest.json"
    registry_path.write_text(yaml.safe_dump(configured), encoding="utf-8")
    manifest_path.write_text(json.dumps({"sources": [{
        "publisher_id": "official-a",
        "url": "https://official-a.example/api/releases/latest",
    }]}), encoding="utf-8")
    try:
        CAPTURE.capture_manifest(manifest_path, registry_path)
        assert False, "missing publisher timestamp field should be rejected"
    except ValueError as exc:
        assert "invalid publisher JSON timestamp field" in str(exc)


def test_capture_manifest_rejects_malformed_configured_json_timestamp_pointer(tmp_path):
    configured = policy()
    configured["publishers"]["official-a"]["published_at_json_pointer"] = 7
    registry_path = tmp_path / "registry.yaml"
    manifest_path = tmp_path / "manifest.json"
    registry_path.write_text(yaml.safe_dump(configured), encoding="utf-8")
    manifest_path.write_text(json.dumps({"sources": [{
        "publisher_id": "official-a",
        "url": "https://official-a.example/api/releases/latest",
        "published_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        "publication_time_attestation": "operator_attested",
    }]}), encoding="utf-8")
    try:
        CAPTURE.capture_manifest(manifest_path, registry_path)
        assert False, "invalid JSON timestamp pointer should be rejected"
    except ValueError as exc:
        assert "invalid configured JSON timestamp pointer" in str(exc)


def test_dns_resolution_timeout_returns_without_waiting_for_blocked_resolver(monkeypatch):
    import time

    def blocked_dns(*args, **kwargs):
        time.sleep(0.5)
        return []

    monkeypatch.setattr(CAPTURE.socket, "getaddrinfo", blocked_dns)
    started = time.monotonic()
    try:
        CAPTURE._resolve_public_addresses("official-a.example", timeout=0.02)
        assert False, "slow DNS must time out"
    except RuntimeError as exc:
        assert "publisher_dns_resolution_timeout" in str(exc)
    assert time.monotonic() - started < 0.2


def test_dns_resolution_rejects_invalid_timeout():
    for timeout in (0, -1, float("inf"), True, 31):
        try:
            CAPTURE._resolve_public_addresses("official-a.example", timeout=timeout)
            assert False, "invalid DNS timeout must be rejected"
        except ValueError as exc:
            assert "DNS timeout" in str(exc)
