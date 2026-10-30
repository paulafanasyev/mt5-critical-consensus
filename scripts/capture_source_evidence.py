#!/usr/bin/env python3
"""Fetch source documents from operator-allowlisted HTTPS publishers and archive exact bytes."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from critical_consensus.persistence import PostgresRepository
from critical_consensus.source_provenance import (
    _host_allowed, source_id_for_capture, validate_source_capture,
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):
        return None


def _safe_url(url: str, profile: dict) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password
            or parsed.fragment or parsed.port not in (None, 443) or is_ip_literal
            or not _host_allowed(host, profile.get("domains", []))):
        raise ValueError("source URL is not an allowlisted HTTPS publisher URL")
    if re.search(r"(?:^|[&;])(token|api[_-]?key|password|secret|signature|authorization)=", parsed.query, re.I):
        raise ValueError("source URL query appears to contain a credential")
    if not parsed.path or parsed.path == "/":
        raise ValueError("source URL must identify a specific publisher resource")
    return host


def _fetch(url: str, *, max_bytes: int, timeout: float) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "MT5-Critical-Consensus-SourceCapture/1.0",
                      "Accept": "text/html,application/json,application/xml,text/plain"},
        method="GET",
    )
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type().lower()
            if not (content_type.startswith("text/") or content_type in {
                "application/json", "application/xml", "application/rss+xml", "application/atom+xml"
            }):
                raise ValueError("publisher response is not a supported text or JSON document")
            declared = response.headers.get("Content-Length")
            if declared:
                try:
                    if int(declared) > max_bytes:
                        raise ValueError("publisher response exceeds the configured size limit")
                except ValueError as exc:
                    if str(exc).startswith("publisher response"):
                        raise
                    raise ValueError("publisher returned an invalid Content-Length") from exc
            body = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"publisher_http_error:{exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError("publisher_network_or_timeout_error") from None
    if not body or len(body) > max_bytes:
        raise ValueError("publisher response is empty or exceeds the configured size limit")
    return body, content_type


def capture_manifest(manifest_path: Path, registry_path: Path, *, timeout: float = 12.0) -> list[dict]:
    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    if not isinstance(registry, dict):
        raise ValueError("source registry policy must be an object")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("sources") if isinstance(manifest, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("manifest must contain a non-empty 'sources' array")
    max_bytes = int(registry.get("max_content_bytes", 512_000))
    now = datetime.now(timezone.utc)
    output = []
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise ValueError(f"source entry {index} must be an object")
        publisher_id = str(item.get("publisher_id", "")).strip()
        publishers = registry.get("publishers", {})
        profile = publishers.get(publisher_id) if isinstance(publishers, dict) else None
        if not isinstance(profile, dict):
            raise ValueError(f"source entry {index} references a publisher not in source_registry.yaml")
        url = str(item.get("url", "")).strip()
        host = _safe_url(url, profile)
        published_at = item.get("published_at")
        attestation = str(item.get("publication_time_attestation", "")).strip()
        allowed_attestations = profile.get("allowed_publication_time_attestations", [])
        if attestation not in allowed_attestations:
            raise ValueError(f"source entry {index} has no allowed publication-time attestation")
        body, content_type = _fetch(url, max_bytes=max_bytes, timeout=timeout)
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        record = {
            "source_id": source_id_for_capture(publisher_id, url, digest),
            "publisher_id": publisher_id,
            "publisher_domain": host,
            "source_url": url,
            "published_at": published_at,
            "retrieved_at": now.isoformat(),
            "independence_group": str(profile.get("independence_group", "")),
            "credibility": profile.get("credibility"),
            "ttl_seconds": profile.get("ttl_seconds"),
            "content_sha256": digest,
            "content_bytes": len(body),
            "captured_content": body,
            "publication_time_attestation": attestation,
            "capture_method": "https_fetch",
            "registry_version": str(registry.get("version", "")),
        }
        validation = validate_source_capture(record, registry, now=now)
        if validation.get("status") != "PASS":
            raise ValueError(
                f"source entry {index} failed provenance policy: " +
                ",".join(validation.get("blockers", []))
            )
        record["response_content_type"] = content_type
        output.append(record)
    return output


def _load_registry(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError("source registry policy must be an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="JSON publisher/url/published_at manifest")
    parser.add_argument("--registry", type=Path,
                        default=Path(__file__).resolve().parents[1] / "config" / "source_registry.yaml")
    parser.add_argument("--timeout", type=float, default=12.0)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 30:
        parser.error("--timeout must be between 1 and 30 seconds")
    try:
        records = capture_manifest(args.manifest, args.registry, timeout=args.timeout)
        repo = PostgresRepository()
        repo.migrate()
        stored = [repo.record_source_evidence(record, registry=_load_registry(args.registry)) for record in records]
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, yaml.YAMLError) as exc:
        parser.error(str(exc))
    print(json.dumps({
        "status": "CAPTURED",
        "captured_count": len(stored),
        "sources": [{"source_id": item["source_id"], "publisher_id": item["publisher_id"],
                     "content_sha256": item["content_sha256"], "content_bytes": item["content_bytes"],
                     "inserted": stored[index]["inserted"],
                     "publication_time_attestation": item["publication_time_attestation"]}
                    for index, item in enumerate(records)],
        "warning": "Publisher domain was allowlisted and response bytes were captured over HTTPS. Publication time remains at the declared attestation level; this is not proof of independent reporting.",
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
