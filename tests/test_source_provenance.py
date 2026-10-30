from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from critical_consensus.source_provenance import (
    source_id_for_capture, validate_persisted_source_set, validate_source_capture,
)


NOW = datetime(2026, 10, 30, 2, 0, tzinfo=timezone.utc)
BODY = b'{"headline":"central bank statement","body":"policy unchanged"}'
DIGEST = "sha256:" + hashlib.sha256(BODY).hexdigest()


def registry():
    return {
        "version": "1",
        "max_clock_skew_seconds": 60,
        "max_capture_age_seconds": 300,
        "max_source_age_seconds": 900,
        "min_independent_publishers": 2,
        "max_content_bytes": 512000,
        "publishers": {
            "central-bank-a": {
                "domains": ["cb-a.example"], "independence_group": "official-bank-a",
                "credibility": 0.98, "ttl_seconds": 900,
                "allowed_publication_time_attestations": ["operator_attested"],
            },
            "news-wire-b": {
                "domains": ["news-b.example"], "independence_group": "news-wire-b",
                "credibility": 0.9, "ttl_seconds": 600,
                "allowed_publication_time_attestations": ["operator_attested"],
            },
        },
    }


def record(publisher_id="central-bank-a", url="https://cb-a.example/releases/1",
           body=BODY, published_at=None, independence_group=None):
    cfg = registry()["publishers"][publisher_id]
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    return {
        "source_id": source_id_for_capture(publisher_id, url, digest, registry_version="1",
                                          published_at=(published_at or (NOW - timedelta(minutes=2)).isoformat()),
                                          publication_time_attestation="operator_attested"),
        "publisher_id": publisher_id,
        "publisher_domain": url.split("/")[2],
        "source_url": url,
        "published_at": published_at or (NOW - timedelta(minutes=2)).isoformat(),
        "retrieved_at": (NOW - timedelta(seconds=5)).isoformat(),
        "independence_group": independence_group or cfg["independence_group"],
        "credibility": cfg["credibility"], "ttl_seconds": cfg["ttl_seconds"],
        "content_sha256": digest, "content_bytes": len(body), "captured_content": body,
        "publication_time_attestation": "operator_attested",
        "capture_method": "https_fetch", "registry_version": "1",
    }


def test_source_capture_requires_allowlisted_https_publisher_and_exact_domain():
    good = validate_source_capture(record(), registry(), now=NOW)
    assert good["status"] == "PASS"
    for bad_url in (
        "http://cb-a.example/releases/1",
        "https://cb-a.example.attacker.invalid/releases/1",
        "https://user:pass@cb-a.example/releases/1",
        "https://127.0.0.1/private",
        "https://cb-a.example:8443/releases/1",
    ):
        item = record(url=bad_url)
        assert validate_source_capture(item, registry(), now=NOW)["status"] == "DENY"


def test_source_capture_requires_actual_bytes_matching_sha256():
    item = record()
    item["captured_content"] = b'{"tampered":true}'
    result = validate_source_capture(item, registry(), now=NOW)
    assert result["status"] == "DENY"
    assert "SOURCE_CAPTURED_CONTENT_HASH_MISMATCH" in result["blockers"]


def test_source_capture_rejects_unregistered_publication_time_attestation():
    item = record()
    item["publication_time_attestation"] = "model_estimate"
    result = validate_source_capture(item, registry(), now=NOW)
    assert "SOURCE_PUBLICATION_TIME_ATTESTATION_NOT_ALLOWED" in result["blockers"]


def test_source_capture_rejects_stale_or_future_published_documents():
    stale = record(published_at=(NOW - timedelta(hours=2)).isoformat())
    future = record(published_at=(NOW + timedelta(minutes=3)).isoformat())
    assert "SOURCE_PUBLICATION_STALE" in validate_source_capture(stale, registry(), now=NOW)["blockers"]
    assert "SOURCE_PUBLICATION_TIME_IN_FUTURE" in validate_source_capture(future, registry(), now=NOW)["blockers"]


def test_persisted_source_set_uses_registry_derived_values_and_requires_independent_groups():
    first = record()
    url = "https://news-b.example/story/2"
    second = record("news-wire-b", url)
    source_rows = [
        {"source_id": first["source_id"], "credibility": 0.01, "age_seconds": 999999},
        {"source_id": second["source_id"], "credibility": 0.01, "age_seconds": 999999},
    ]
    result = validate_persisted_source_set(
        source_rows, [first, second], registry(), now=NOW,
    )
    assert result["status"] == "PASS"
    assert len(result["sources"]) == 2
    assert result["sources"][0]["credibility"] == 0.98
    assert result["sources"][0]["age_seconds"] == 120
    assert len(result["independence_groups"]) == 2


def test_persisted_source_set_rejects_same_independence_group_and_id_mismatch():
    first = record()
    second = record("news-wire-b", "https://news-b.example/story/2", independence_group="official-bank-a")
    sources = [{"source_id": first["source_id"]}, {"source_id": second["source_id"]}]
    result = validate_persisted_source_set(sources, [first, second], registry(), now=NOW)
    assert result["status"] == "DENY"
    assert "INSUFFICIENT_INDEPENDENT_PUBLISHER_GROUPS" in result["blockers"]
    mismatch = validate_persisted_source_set(
        [{"source_id": first["source_id"]}, {"source_id": "src-missing"}],
        [first, second], registry(), now=NOW,
    )
    assert "PERSISTED_SOURCE_ID_SET_MISMATCH" in mismatch["blockers"]


def test_empty_publisher_allowlist_fails_closed():
    item = record()
    empty = dict(registry(), publishers={})
    result = validate_persisted_source_set(
        [{"source_id": item["source_id"]}], [item], empty, now=NOW,
    )
    assert result["status"] == "DENY"
    assert any("SOURCE_PUBLISHER_NOT_ALLOWLISTED" in blocker for blocker in result["blockers"])


def test_generic_https_capture_only_accepts_operator_attested_publication_time():
    item = record()
    policy = registry()
    profile = policy["publishers"]["central-bank-a"]
    profile["allowed_publication_time_attestations"].append("publisher_api_field")
    item["publication_time_attestation"] = "publisher_api_field"
    result = validate_source_capture(item, policy, now=NOW)
    assert result["status"] == "DENY"
    assert "SOURCE_PUBLICATION_TIME_ATTESTATION_NOT_ALLOWED" in result["blockers"]
