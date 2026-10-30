#!/usr/bin/env python3
"""Fetch source documents from operator-allowlisted HTTPS publishers and archive exact bytes."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import yaml

from critical_consensus.persistence import PostgresRepository
from critical_consensus.source_provenance import (
    _host_allowed, _json_pointer_value, _stamp, source_id_for_capture, validate_source_capture,
)


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
    allowed_query_parameters = profile.get("allowed_query_parameters", [])
    if not isinstance(allowed_query_parameters, list) or any(not isinstance(key, str) for key in allowed_query_parameters):
        raise ValueError("publisher allowed_query_parameters policy is invalid")
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    forbidden_names = {"token", "access_token", "api_key", "apikey", "password", "secret",
                       "signature", "authorization", "client_secret"}
    if (any(name.lower() in forbidden_names for name, _ in query_pairs)
            or any(name not in allowed_query_parameters for name, _ in query_pairs)):
        raise ValueError("source URL has an unapproved or credential-like query parameter")
    if not parsed.path or parsed.path == "/":
        raise ValueError("source URL must identify a specific publisher resource")
    return host


_DNS_RESOLUTION_SLOTS = threading.BoundedSemaphore(2)


def _resolve_public_addresses(host: str, *, timeout: float = 5.0) -> list[str]:
    """Resolve once with a hard caller deadline, reject private answers, and pin HTTPS."""
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0 or timeout > 30):
        raise ValueError("DNS timeout must be finite and in (0,30]")
    if not _DNS_RESOLUTION_SLOTS.acquire(timeout=float(timeout)):
        raise RuntimeError("publisher_dns_resolution_capacity_exhausted")
    completed = threading.Event()
    outcome = {}

    def lookup():
        try:
            outcome["answers"] = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except Exception as exc:
            outcome["error"] = exc
        finally:
            _DNS_RESOLUTION_SLOTS.release()
            completed.set()

    worker = threading.Thread(target=lookup, name="cc-publisher-dns", daemon=True)
    try:
        worker.start()
    except Exception as exc:
        _DNS_RESOLUTION_SLOTS.release()
        raise RuntimeError("publisher_dns_worker_start_failed") from None
    if not completed.wait(float(timeout)):
        raise RuntimeError("publisher_dns_resolution_timeout")
    error = outcome.get("error")
    if isinstance(error, socket.gaierror):
        raise RuntimeError("publisher_dns_resolution_failed") from None
    if error is not None:
        raise RuntimeError("publisher_dns_resolution_failed") from None
    answers = outcome.get("answers", [])
    addresses: list[str] = []
    for answer in answers:
        raw_ip = str(answer[4][0]).split("%", 1)[0]
        try:
            parsed = ipaddress.ip_address(raw_ip)
        except ValueError:
            raise RuntimeError("publisher_dns_returned_invalid_address") from None
        if not parsed.is_global:
            raise ValueError("publisher DNS resolved to a non-public network address")
        normalized = parsed.compressed
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise RuntimeError("publisher_dns_returned_no_addresses")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to a previously validated public IP, retaining host SNI/cert checks."""

    def __init__(self, hostname: str, pinned_ip: str, *, timeout: float):
        super().__init__(hostname, port=443, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        if self._tunnel_host:
            raise OSError("HTTPS proxy tunnels are disabled for publisher capture")
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def _fetch(url: str, *, max_bytes: int, timeout: float) -> tuple[bytes, str]:
    if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
            or not 1 <= max_bytes <= 512_000):
        raise ValueError("max_bytes must be an integer between 1 and 512000")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 1 <= timeout <= 30):
        raise ValueError("timeout must be finite and between 1 and 30 seconds")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.port not in (None, 443):
        raise ValueError("publisher URL must be HTTPS on the default port")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    addresses = _resolve_public_addresses(host)
    headers = {
        "User-Agent": "MT5-CriticalConsensus-SourceCapture/1.0",
        "Accept": "text/html,application/json,application/xml,text/plain",
        "Connection": "close",
    }
    deadline = time.monotonic() + float(timeout)
    last_network_error: OSError | None = None
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        connection = _PinnedHTTPSConnection(host, address, timeout=remaining)
        response = None
        try:
            connection.request("GET", path, headers=headers)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("publisher_response_deadline_exceeded")
            sock = getattr(connection, "sock", None)
            if sock is not None:
                sock.settimeout(remaining)
            response = connection.getresponse()
            if response.status < 200 or response.status >= 300:
                status = response.status
                raise RuntimeError(f"publisher_http_error:{status}")
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if not (content_type.startswith("text/") or content_type in {
                "application/json", "application/xml", "application/rss+xml", "application/atom+xml"
            }):
                raise ValueError("publisher response is not a supported text or JSON document")
            declared = response.getheader("Content-Length")
            declared_length = None
            if declared is not None:
                try:
                    declared_length = int(declared)
                except ValueError:
                    raise ValueError("publisher returned an invalid Content-Length") from None
                if declared_length < 0 or declared_length > max_bytes:
                    raise ValueError("publisher response exceeds the configured size limit")
            reader = getattr(response, "read1", None)
            if callable(reader):
                body_buffer = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("publisher_response_deadline_exceeded")
                    sock = getattr(connection, "sock", None)
                    if sock is not None:
                        sock.settimeout(remaining)
                    chunk = reader(min(64_000, max_bytes + 1 - len(body_buffer)))
                    if not chunk:
                        break
                    body_buffer.extend(chunk)
                    if len(body_buffer) > max_bytes:
                        raise ValueError("publisher response is empty or exceeds the configured size limit")
                body = bytes(body_buffer)
            else:
                # Compatibility for test doubles; the real http.client response exposes read1.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("publisher_response_deadline_exceeded")
                sock = getattr(connection, "sock", None)
                if sock is not None:
                    sock.settimeout(remaining)
                body = response.read(max_bytes + 1)
            if not body or len(body) > max_bytes:
                raise ValueError("publisher response is empty or exceeds the configured size limit")
            if declared_length is not None and len(body) != declared_length:
                raise RuntimeError("publisher_response_content_length_mismatch")
            return body, content_type
        except (OSError, http.client.HTTPException) as exc:
            last_network_error = exc
        finally:
            if response is not None:
                response.close()
            connection.close()
    if last_network_error is not None:
        raise RuntimeError("publisher_network_or_tls_error") from None
    raise RuntimeError("publisher_fetch_failed")


def capture_manifest(manifest_path: Path, registry_path: Path, *, timeout: float = 12.0) -> list[dict]:
    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    if not isinstance(registry, dict):
        raise ValueError("source registry policy must be an object")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("sources") if isinstance(manifest, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("manifest must contain a non-empty 'sources' array")
    if len(entries) > 13:
        raise ValueError("manifest may contain at most 13 source records per capture batch")
    raw_max_bytes = registry.get("max_content_bytes", 512_000)
    if (isinstance(raw_max_bytes, bool) or not isinstance(raw_max_bytes, int)
            or not 1 <= raw_max_bytes <= 512_000):
        raise ValueError("max_content_bytes must be an integer between 1 and 512000")
    max_bytes = raw_max_bytes
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
        has_json_timestamp_pointer = "published_at_json_pointer" in profile
        configured_pointer = profile.get("published_at_json_pointer")
        if has_json_timestamp_pointer and (
            not isinstance(configured_pointer, str) or not configured_pointer.startswith("/")
            or configured_pointer == "/"
        ):
            raise ValueError(f"source entry {index} has an invalid configured JSON timestamp pointer")
        body, content_type = _fetch(url, max_bytes=max_bytes, timeout=timeout)
        if has_json_timestamp_pointer:
            publication_time_field = configured_pointer
            if content_type != "application/json":
                raise ValueError(f"source entry {index} JSON timestamp pointer requires application/json")
            try:
                document = json.loads(body.decode("utf-8-sig"))
                pointer_value = _json_pointer_value(document, publication_time_field)
                pointer_stamp = _stamp(pointer_value)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                raise ValueError(f"source entry {index} has an invalid publisher JSON timestamp field") from None
            if pointer_stamp is None:
                raise ValueError(f"source entry {index} publisher JSON timestamp is not timezone-aware ISO-8601")
            published_at = pointer_stamp.isoformat()
            provided_published_at = item.get("published_at")
            if provided_published_at is not None:
                provided_stamp = _stamp(provided_published_at)
                if provided_stamp is None or provided_stamp != pointer_stamp:
                    raise ValueError(f"source entry {index} manifest date does not match publisher JSON timestamp")
            attestation = "publisher_json_pointer"
        else:
            publication_time_field = ""
            published_at = item.get("published_at")
            attestation = str(item.get("publication_time_attestation", "")).strip()
            if attestation != "operator_attested":
                raise ValueError(f"source entry {index} requires operator_attested publication-time attestation")
        allowed_attestations = profile.get("allowed_publication_time_attestations", [])
        if not isinstance(allowed_attestations, list) or attestation not in allowed_attestations:
            raise ValueError(f"source entry {index} has no allowed publication-time attestation")
        if _stamp(published_at) is None:
            raise ValueError(f"source entry {index} publication time must be timezone-aware ISO-8601")
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        record = {
            "source_id": source_id_for_capture(
                publisher_id, url, digest,
                registry_version=str(registry.get("version", "")),
                published_at=str(published_at),
                publication_time_attestation=attestation,
                publication_time_field=publication_time_field,
            ),
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
            "response_content_type": content_type,
            "publication_time_attestation": attestation,
            "publication_time_field": publication_time_field,
            "capture_method": "https_fetch",
            "registry_version": str(registry.get("version", "")),
        }
        validation = validate_source_capture(record, registry, now=now)
        if validation.get("status") != "PASS":
            raise ValueError(
                f"source entry {index} failed provenance policy: " +
                ",".join(validation.get("blockers", []))
            )
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
