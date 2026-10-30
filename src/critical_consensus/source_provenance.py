"""Verified publisher-origin and source-freshness checks for persisted evidence.

A registry entry is an operator-managed trust decision, not a model confidence score.
A successful HTTPS capture proves which allowlisted endpoint supplied the captured bytes;
publication-time metadata may still be operator-attested and must be labelled as such.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _stamp(value: Any) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _host_allowed(host: str, domains: Sequence[str]) -> bool:
    host = host.lower().rstrip(".")
    if not isinstance(domains, Sequence) or isinstance(domains, (str, bytes)):
        return False
    for domain in domains:
        if not isinstance(domain, str) or not domain.strip():
            continue
        normalized = domain.strip().lower().rstrip(".")
        if host == normalized or host.endswith("." + normalized):
            return True
    return False


def source_id_for_capture(
    publisher_id: str, url: str, content_sha256: str, *,
    registry_version: str = "", published_at: str = "", publication_time_attestation: str = "",
) -> str:
    """Derive an immutable ID from publisher, bytes, policy version and date attestation."""
    published_stamp = _stamp(published_at)
    canonical_published_at = published_stamp.isoformat() if published_stamp is not None else str(published_at)
    payload = json.dumps(
        {"publisher_id": publisher_id, "url": url, "content_sha256": content_sha256,
         "registry_version": registry_version, "published_at": canonical_published_at,
         "publication_time_attestation": publication_time_attestation},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return "src-" + digest[:40]


def validate_source_capture(
    record: Mapping[str, Any], registry: Mapping[str, Any], *, now: datetime | None = None,
) -> dict[str, Any]:
    """Validate a persisted source record against explicit publisher allowlist configuration."""
    if not isinstance(record, Mapping) or not isinstance(registry, Mapping):
        return {"status": "DENY", "blockers": ["SOURCE_RECORD_OR_REGISTRY_INVALID"], "trade_allowed": False}
    current = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        return {"status": "DENY", "blockers": ["SOURCE_VALIDATION_TIME_INVALID"], "trade_allowed": False}
    current = current.astimezone(timezone.utc)
    blockers: list[str] = []
    publishers = registry.get("publishers")
    publisher_id = str(record.get("publisher_id", "")).strip()
    profile = publishers.get(publisher_id) if isinstance(publishers, Mapping) else None
    if not isinstance(profile, Mapping):
        blockers.append("SOURCE_PUBLISHER_NOT_ALLOWLISTED")
        profile = {}
    domains = profile.get("domains", [])
    if (not isinstance(domains, Sequence) or isinstance(domains, (str, bytes)) or not domains
            or any(not isinstance(domain, str) or not domain.strip() for domain in domains)):
        blockers.append("SOURCE_PUBLISHER_DOMAIN_POLICY_MISSING")
        domains = []

    raw_url = str(record.get("source_url", "")).strip()
    try:
        parsed = urlsplit(raw_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        try:
            ipaddress.ip_address(host)
            is_ip_literal = True
        except ValueError:
            is_ip_literal = False
        if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                or parsed.fragment or parsed.port not in (None, 443) or is_ip_literal
                or not _host_allowed(host, domains)):
            blockers.append("SOURCE_URL_NOT_HTTPS_OR_PUBLISHER_HOST_MISMATCH")
    except ValueError:
        host = ""
        blockers.append("SOURCE_URL_INVALID")
    stored_host = str(record.get("publisher_domain", "")).strip().lower().rstrip(".")
    if not host or stored_host != host:
        blockers.append("SOURCE_PUBLISHER_DOMAIN_MISMATCH")

    expected_group = str(profile.get("independence_group", "")).strip()
    if not expected_group or str(record.get("independence_group", "")).strip() != expected_group:
        blockers.append("SOURCE_INDEPENDENCE_GROUP_MISMATCH")

    credibility = _number(profile.get("credibility"))
    stored_credibility = _number(record.get("credibility"))
    if (credibility is None or not 0 <= credibility <= 1 or stored_credibility is None
            or abs(credibility - stored_credibility) > 1e-12):
        blockers.append("SOURCE_CREDIBILITY_NOT_REGISTRY_DERIVED")
    ttl = _number(profile.get("ttl_seconds"))
    stored_ttl = _number(record.get("ttl_seconds"))
    if (ttl is None or ttl <= 0 or stored_ttl is None or stored_ttl != ttl or not ttl.is_integer()):
        blockers.append("SOURCE_TTL_NOT_REGISTRY_DERIVED")

    published = _stamp(record.get("published_at"))
    retrieved = _stamp(record.get("retrieved_at"))
    if published is None:
        blockers.append("SOURCE_PUBLICATION_TIME_INVALID")
    if retrieved is None:
        blockers.append("SOURCE_RETRIEVAL_TIME_INVALID")
    if published is not None and retrieved is not None:
        max_future = _number(registry.get("max_clock_skew_seconds", 60))
        max_capture_age = _number(registry.get("max_capture_age_seconds", 300))
        max_source_age = _number(registry.get("max_source_age_seconds", 900))
        if max_future is None or max_future < 0:
            blockers.append("SOURCE_CLOCK_SKEW_POLICY_INVALID")
            max_future = 60
        if max_capture_age is None or max_capture_age <= 0:
            blockers.append("SOURCE_CAPTURE_AGE_POLICY_INVALID")
            max_capture_age = 300
        if max_source_age is None or max_source_age <= 0:
            blockers.append("SOURCE_MAX_AGE_POLICY_INVALID")
            max_source_age = 900
        if (published - retrieved).total_seconds() > max_future:
            blockers.append("SOURCE_PUBLICATION_TIME_IN_FUTURE")
        if (retrieved - current).total_seconds() > max_future:
            blockers.append("SOURCE_RETRIEVAL_TIME_IN_FUTURE")
        if (current - retrieved).total_seconds() > max_capture_age:
            blockers.append("SOURCE_CAPTURE_STALE")
        age = (current - published).total_seconds()
        max_age = min(float(ttl or 0), max_source_age)
        if age < -max_future:
            blockers.append("SOURCE_PUBLICATION_TIME_IN_FUTURE")
        elif age > max_age:
            blockers.append("SOURCE_PUBLICATION_STALE")
    else:
        age = None

    attestation = str(record.get("publication_time_attestation", "")).strip()
    allowed_attestations = profile.get("allowed_publication_time_attestations", [])
    if (not isinstance(allowed_attestations, Sequence) or isinstance(allowed_attestations, (str, bytes))
            or attestation != "operator_attested" or attestation not in allowed_attestations):
        blockers.append("SOURCE_PUBLICATION_TIME_ATTESTATION_NOT_ALLOWED")

    digest = str(record.get("content_sha256", ""))
    if _SHA256_RE.fullmatch(digest) is None:
        blockers.append("SOURCE_CONTENT_HASH_INVALID")
    captured_content = record.get("captured_content")
    if not isinstance(captured_content, (bytes, bytearray, memoryview)) or not captured_content:
        blockers.append("SOURCE_CAPTURED_BYTES_MISSING")
        actual_bytes = b""
    else:
        actual_bytes = bytes(captured_content)
    actual_digest = "sha256:" + hashlib.sha256(actual_bytes).hexdigest() if actual_bytes else ""
    if actual_digest != digest:
        blockers.append("SOURCE_CAPTURED_CONTENT_HASH_MISMATCH")
    try:
        content_bytes = int(record.get("content_bytes"))
    except (TypeError, ValueError, OverflowError):
        content_bytes = 0
    max_content_value = _number(registry.get("max_content_bytes", 512_000))
    if (max_content_value is None or max_content_value < 1 or not max_content_value.is_integer()):
        blockers.append("SOURCE_MAX_CONTENT_POLICY_INVALID")
        max_content = 512_000
    else:
        max_content = int(max_content_value)
    if content_bytes < 1 or content_bytes > max_content or content_bytes != len(actual_bytes):
        blockers.append("SOURCE_CAPTURE_SIZE_INVALID")
    if str(record.get("capture_method", "")) != "https_fetch":
        blockers.append("SOURCE_CAPTURE_METHOD_NOT_VERIFIED")
    registry_version = str(registry.get("version", "")).strip()
    if str(record.get("registry_version", "")).strip() != registry_version or not registry_version:
        blockers.append("SOURCE_REGISTRY_VERSION_MISMATCH")

    expected_source_id = source_id_for_capture(
        publisher_id, raw_url, digest,
        registry_version=str(registry.get("version", "")),
        published_at=published.isoformat() if published is not None else "",
        publication_time_attestation=attestation,
    ) if _SHA256_RE.fullmatch(digest) else ""
    if not expected_source_id or str(record.get("source_id", "")) != expected_source_id:
        blockers.append("SOURCE_ID_CONTENT_BINDING_MISMATCH")

    if blockers:
        return {"status": "DENY", "blockers": sorted(set(blockers)), "trade_allowed": False}
    return {
        "status": "PASS", "blockers": [], "trade_allowed": False,
        "source_id": expected_source_id, "publisher_id": publisher_id, "publisher_domain": host,
        "independence_group": expected_group, "credibility": float(credibility),
        "age_seconds": max(0.0, float(age)), "ttl_seconds": int(ttl),
        "content_sha256": digest, "publication_time_attestation": attestation,
    }


def validate_persisted_source_set(
    key_sources: Any, persisted_records: Any, registry: Mapping[str, Any], *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve every requested source ID to an immutable DB record and normalize trust fields."""
    if (not isinstance(key_sources, Sequence) or isinstance(key_sources, (str, bytes)) or not key_sources
            or any(not isinstance(item, Mapping) for item in key_sources)):
        return {"status": "DENY", "blockers": ["KEY_SOURCE_SET_MISSING_OR_INVALID"],
                "sources": [], "trade_allowed": False}
    if (not isinstance(persisted_records, Sequence) or isinstance(persisted_records, (str, bytes))
            or not persisted_records or any(not isinstance(item, Mapping) for item in persisted_records)):
        return {"status": "DENY", "blockers": ["PERSISTED_SOURCE_EVIDENCE_MISSING_OR_INVALID"],
                "sources": [], "trade_allowed": False}
    requested_ids = [str(item.get("source_id", "")).strip() for item in key_sources]
    persisted_ids = [str(item.get("source_id", "")).strip() for item in persisted_records]
    if (any(not item for item in requested_ids + persisted_ids)
            or len(set(requested_ids)) != len(requested_ids)
            or len(set(persisted_ids)) != len(persisted_ids)
            or set(requested_ids) != set(persisted_ids)):
        return {"status": "DENY", "blockers": ["PERSISTED_SOURCE_ID_SET_MISMATCH"],
                "sources": [], "trade_allowed": False}

    by_id = {str(item["source_id"]): item for item in persisted_records}
    normalized: list[dict[str, Any]] = []
    blockers: list[str] = []
    for source_id in requested_ids:
        checked = validate_source_capture(by_id[source_id], registry, now=now)
        if checked.get("status") != "PASS":
            blockers.extend(f"{source_id}:{reason}" for reason in checked.get("blockers", []))
        else:
            normalized.append({key: checked[key] for key in (
                "source_id", "credibility", "age_seconds", "ttl_seconds", "independence_group"
            )})
    minimum_groups = registry.get("min_independent_publishers", 2)
    if isinstance(minimum_groups, bool) or not isinstance(minimum_groups, int) or minimum_groups < 2:
        blockers.append("SOURCE_MINIMUM_INDEPENDENCE_POLICY_INVALID")
        minimum_groups = 2
    groups = {item["independence_group"] for item in normalized}
    if len(groups) < minimum_groups:
        blockers.append("INSUFFICIENT_INDEPENDENT_PUBLISHER_GROUPS")
    if blockers:
        return {"status": "DENY", "blockers": sorted(set(blockers)),
                "sources": [], "independence_groups": sorted(groups), "trade_allowed": False}
    return {
        "status": "PASS", "blockers": [], "sources": normalized,
        "independence_groups": sorted(groups), "trade_allowed": False,
    }
