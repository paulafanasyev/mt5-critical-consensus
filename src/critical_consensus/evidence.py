"""HMAC-authenticated evidence envelopes for trusted ingestion hand-offs.

The bridge and CLI must never treat an arbitrary JSON object as agent-produced
evidence. A trusted ingress signs the exact canonical payload with a shared secret;
the pipeline verifies issuer, freshness, payload hash and signature before any gate
consumes it. HMAC proves that the holder of the key produced the envelope; it does
not prove the correctness of the upstream analysis.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import secrets
from typing import Any, Mapping

MIN_KEY_BYTES = 32
MAX_KEY_BYTES = 256
ENVELOPE_VERSION = 1
DEFAULT_MAX_AGE_SECONDS = 300
MAX_TTL_SECONDS = 3600
DEFAULT_FUTURE_SKEW_SECONDS = 30
_SIGNATURE_PREFIX = "hmac-sha256:"
_SIGNATURE_CONTEXT = b"critical-consensus-envelope-v1:"
_DOMAIN_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SIGNATURE_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_ENVELOPE_FIELDS = frozenset({
    "schema_version", "issuer", "issued_at", "nonce", "ttl_seconds",
    "payload_sha256", "signature",
})


class EvidenceError(ValueError):
    """Raised when an envelope cannot be created or parsed safely."""


def _key_bytes(key: str | bytes | bytearray | None) -> bytes:
    if isinstance(key, str):
        encoded = key.encode("utf-8")
    elif isinstance(key, (bytes, bytearray)):
        encoded = bytes(key)
    else:
        raise EvidenceError("signing key must be a string or bytes value")
    if not MIN_KEY_BYTES <= len(encoded) <= MAX_KEY_BYTES:
        raise EvidenceError(f"signing key must contain {MIN_KEY_BYTES}..{MAX_KEY_BYTES} bytes")
    return encoded


def _reject_non_string_keys(value: Any, path: str = "payload") -> None:
    """JSON object keys must be strings; int keys would silently collide after encoding."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EvidenceError(f"{path} contains a non-string object key")
            _reject_non_string_keys(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_non_string_keys(item, f"{path}[{index}]")


def canonical_bytes(payload: Any) -> bytes:
    """Strict canonical JSON; no string coercion means hash collisions are not possible."""
    try:
        _reject_non_string_keys(payload)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False)
        return raw.encode("utf-8")
    except EvidenceError:
        raise
    except RecursionError as exc:
        # A deeply nested payload exhausts the C encoder's stack; that must surface as a
        # fail-closed evidence error (HTTP 4xx), never as an unhandled 500.
        raise EvidenceError("payload is nested too deeply") from exc
    except (TypeError, ValueError, UnicodeError) as exc:
        # Type errors (non-JSON values), NaN/Infinity and lone surrogates all have to reach
        # callers as EvidenceError so verification fails closed instead of crashing.
        raise EvidenceError("payload is not canonically serializable") from exc


def payload_sha256(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _parse_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvidenceError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _current_utc(now: datetime | None) -> datetime:
    current = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise EvidenceError("current time must be timezone-aware")
    return current.astimezone(timezone.utc)


def key_bytes(key: str | bytes | bytearray | None) -> bytes:
    """Public normalization used to reject shared material across signing domains."""
    return _key_bytes(key)


def _domain_bytes(domain: str) -> bytes:
    if not isinstance(domain, str) or _DOMAIN_RE.fullmatch(domain) is None:
        raise EvidenceError("signing domain must be a lowercase identifier")
    return domain.encode("ascii")


def _signature(envelope_without_signature: Mapping[str, Any], key: bytes, domain: str) -> str:
    message = _SIGNATURE_CONTEXT + _domain_bytes(domain) + b"\n" + canonical_bytes(envelope_without_signature)
    digest = hmac.new(key, message, hashlib.sha256).hexdigest()
    return _SIGNATURE_PREFIX + digest


def make_envelope(
    payload: Any,
    *,
    key: str | bytes | bytearray | None,
    issuer: str,
    issued_at: datetime | None = None,
    nonce: str | None = None,
    ttl_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    domain: str = "evidence",
) -> dict[str, Any]:
    """Create a signed envelope for an exact payload object.

    ``domain`` is part of the signed message, so evidence envelopes and governance
    attestations signed with the same key are still not interchangeable.
    """
    key_bytes = _key_bytes(key)
    _domain_bytes(domain)
    issuer = str(issuer).strip()
    if not issuer or len(issuer) > 128:
        raise EvidenceError("issuer must be a non-empty string of at most 128 characters")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
        raise EvidenceError(f"ttl_seconds must be an integer in [1,{MAX_TTL_SECONDS}]")
    issued = _current_utc(issued_at)
    token = nonce if nonce is not None else secrets.token_hex(16)
    if not isinstance(token, str) or _NONCE_RE.fullmatch(token) is None:
        raise EvidenceError("nonce must be 16..128 URL-safe characters")
    envelope: dict[str, Any] = {
        "schema_version": ENVELOPE_VERSION,
        "issuer": issuer,
        "issued_at": issued.isoformat(),
        "nonce": token,
        "ttl_seconds": ttl_seconds,
        "payload_sha256": payload_sha256(payload),
    }
    envelope["signature"] = _signature(envelope, key_bytes, domain)
    return envelope


def verify_envelope(
    payload: Any,
    envelope: Mapping[str, Any] | None,
    *,
    key: str | bytes | bytearray | None,
    expected_issuer: str | None = None,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    future_skew_seconds: int = DEFAULT_FUTURE_SKEW_SECONDS,
    domain: str = "evidence",
) -> dict[str, Any]:
    """Verify signature, payload binding, issuer and freshness. Never raises on bad input."""
    blockers: list[str] = []
    result: dict[str, Any] = {
        "valid": False, "blockers": blockers, "payload_sha256": None,
        "issuer": None, "nonce": None, "issued_at": None,
    }
    try:
        key_bytes = _key_bytes(key)
    except EvidenceError:
        blockers.append("EVIDENCE_KEY_NOT_CONFIGURED_OR_TOO_SHORT")
        return result
    if not isinstance(envelope, Mapping):
        blockers.append("EVIDENCE_ENVELOPE_MISSING_OR_INVALID")
        return result
    if set(envelope) != _ENVELOPE_FIELDS:
        blockers.append("EVIDENCE_ENVELOPE_FIELDS_INVALID")
        return result

    if envelope.get("schema_version") != ENVELOPE_VERSION:
        blockers.append("EVIDENCE_ENVELOPE_VERSION_UNSUPPORTED")
    issuer = envelope.get("issuer")
    if not isinstance(issuer, str) or not issuer.strip() or len(issuer) > 128:
        blockers.append("EVIDENCE_ISSUER_INVALID")
    else:
        result["issuer"] = issuer
        if expected_issuer is not None and issuer != str(expected_issuer).strip():
            blockers.append("EVIDENCE_ISSUER_MISMATCH")

    nonce = envelope.get("nonce")
    if not isinstance(nonce, str) or _NONCE_RE.fullmatch(nonce) is None:
        blockers.append("EVIDENCE_NONCE_INVALID")
    else:
        result["nonce"] = nonce

    ttl_raw = envelope.get("ttl_seconds")
    ttl_seconds: int | None = None
    if isinstance(ttl_raw, bool) or not isinstance(ttl_raw, int) or not 1 <= ttl_raw <= MAX_TTL_SECONDS:
        blockers.append("EVIDENCE_TTL_INVALID")
    else:
        ttl_seconds = ttl_raw

    try:
        current = _current_utc(now)
    except EvidenceError:
        blockers.append("EVIDENCE_CURRENT_TIME_INVALID")
        current = None
    issued_at: datetime | None = None
    try:
        issued_at = _parse_utc(envelope.get("issued_at"), "issued_at")
        result["issued_at"] = issued_at.isoformat()
    except EvidenceError:
        blockers.append("EVIDENCE_ISSUED_AT_INVALID")
    if current is not None and issued_at is not None:
        age = (current - issued_at).total_seconds()
        if age < -int(future_skew_seconds):
            blockers.append("EVIDENCE_ISSUED_AT_IN_FUTURE")
        elif ttl_seconds is not None and age > min(int(max_age_seconds), ttl_seconds):
            blockers.append("EVIDENCE_EXPIRED")

    supplied_hash = envelope.get("payload_sha256")
    if not isinstance(supplied_hash, str) or _HASH_RE.fullmatch(supplied_hash) is None:
        blockers.append("EVIDENCE_PAYLOAD_HASH_INVALID")
    else:
        result["payload_sha256"] = supplied_hash
        try:
            actual_hash = payload_sha256(payload)
        except EvidenceError:
            blockers.append("EVIDENCE_PAYLOAD_NOT_CANONICALLY_HASHABLE")
        else:
            if not hmac.compare_digest(actual_hash, supplied_hash):
                blockers.append("EVIDENCE_PAYLOAD_HASH_MISMATCH")

    signature = envelope.get("signature")
    # The regex is not cosmetic: compare_digest raises TypeError on non-ASCII strings,
    # which would turn a hostile envelope into an unhandled 500 instead of a deny.
    if not isinstance(signature, str) or _SIGNATURE_RE.fullmatch(signature) is None:
        blockers.append("EVIDENCE_SIGNATURE_INVALID")
    else:
        unsigned = {field: value for field, value in envelope.items() if field != "signature"}
        try:
            expected = _signature(unsigned, key_bytes, domain)
        except EvidenceError:
            blockers.append("EVIDENCE_SIGNATURE_INVALID")
        else:
            if not hmac.compare_digest(signature, expected):
                blockers.append("EVIDENCE_SIGNATURE_MISMATCH")

    result["blockers"] = sorted(set(blockers))
    result["valid"] = not blockers
    return result


REVIEW_DOMAIN = "review"


def sign_review(payload: Any, *, key: str | bytes | bytearray | None, issuer: str,
                issued_at: datetime | None = None, nonce: str | None = None,
                ttl_seconds: int = DEFAULT_MAX_AGE_SECONDS) -> dict[str, Any]:
    """Sign review-service/Chief output in its own HMAC domain.

    Evidence ingress must not be able to mint "independent" peer reviews or a Chief
    CONFIRM: those records are only accepted when the review service signed them.
    """
    return make_envelope(payload, key=key, issuer=issuer, issued_at=issued_at,
                         nonce=nonce, ttl_seconds=ttl_seconds, domain=REVIEW_DOMAIN)


def verify_review(payload: Any, envelope: Mapping[str, Any] | None, *,
                  key: str | bytes | bytearray | None, expected_issuer: str | None,
                  now: datetime | None = None,
                  max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS) -> dict[str, Any]:
    result = verify_envelope(payload, envelope, key=key, expected_issuer=expected_issuer,
                             now=now, max_age_seconds=max_age_seconds, domain=REVIEW_DOMAIN)
    # Envelope diagnostics are domain-agnostic; relabel them so an operator can tell a
    # broken review-service attestation apart from a broken ingress evidence envelope.
    result["blockers"] = [blocker.replace("EVIDENCE_", "REVIEW_", 1)
                          for blocker in result.get("blockers", [])]
    return result


def bundle_body(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Signed portion of a decision bundle; operator approval is authenticated separately."""
    if not isinstance(bundle, Mapping):
        raise EvidenceError("bundle must be an object")
    return {key: value for key, value in bundle.items()
            if key not in ("evidence_envelope", "user_decision", "user_decision_envelope")}


def sign_bundle(
    bundle: Mapping[str, Any],
    *,
    key: str | bytes | bytearray | None,
    issuer: str,
    issued_at: datetime | None = None,
    nonce: str | None = None,
    ttl_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    domain: str = "evidence",
) -> dict[str, Any]:
    """Return a copy of the bundle carrying a fresh envelope over everything but user_decision."""
    body = bundle_body(bundle)
    envelope = make_envelope(body, key=key, issuer=issuer, issued_at=issued_at,
                             nonce=nonce, ttl_seconds=ttl_seconds, domain=domain)
    result = dict(bundle)
    result["evidence_envelope"] = envelope
    return result


def verify_bundle(
    bundle: Mapping[str, Any],
    *,
    key: str | bytes | bytearray | None,
    expected_issuer: str | None = None,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    future_skew_seconds: int = DEFAULT_FUTURE_SKEW_SECONDS,
    domain: str = "evidence",
) -> dict[str, Any]:
    """Verify the bundle envelope against the canonical bundle body."""
    if not isinstance(bundle, Mapping):
        return {"valid": False, "blockers": ["EVIDENCE_BUNDLE_INVALID"]}
    try:
        body = bundle_body(bundle)
    except EvidenceError:
        return {"valid": False, "blockers": ["EVIDENCE_BUNDLE_INVALID"]}
    return verify_envelope(
        body, bundle.get("evidence_envelope"), key=key, expected_issuer=expected_issuer,
        now=now, max_age_seconds=max_age_seconds, future_skew_seconds=future_skew_seconds,
        domain=domain,
    )


def verify_wrapper(
    wrapper: Mapping[str, Any] | None,
    *,
    key: str | bytes | bytearray | None,
    expected_issuer: str | None = None,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    future_skew_seconds: int = DEFAULT_FUTURE_SKEW_SECONDS,
    domain: str = "evidence",
) -> dict[str, Any]:
    """Verify a ``{"payload": ..., "envelope": ...}`` wrapper supplied by an HTTP client."""
    if not isinstance(wrapper, Mapping) or set(wrapper) != {"payload", "envelope"}:
        return {"valid": False, "blockers": ["EVIDENCE_WRAPPER_INVALID"]}
    return verify_envelope(
        wrapper["payload"], wrapper["envelope"], key=key, expected_issuer=expected_issuer,
        now=now, max_age_seconds=max_age_seconds, future_skew_seconds=future_skew_seconds,
        domain=domain,
    )


OPERATOR_DOMAIN = "operator"


def sign_decision(
    decision: Mapping[str, Any],
    *,
    key: str | bytes | bytearray | None,
    issuer: str,
    issued_at: datetime | None = None,
    nonce: str | None = None,
    ttl_seconds: int = 120,
) -> dict[str, Any]:
    """Sign a human operator approval in the operator domain, never the evidence domain."""
    if not isinstance(decision, Mapping):
        raise EvidenceError("decision must be an object")
    return make_envelope(decision, key=key, issuer=issuer, issued_at=issued_at, nonce=nonce,
                         ttl_seconds=ttl_seconds, domain=OPERATOR_DOMAIN)


def verify_decision(
    decision: Any,
    envelope: Mapping[str, Any] | None,
    *,
    key: str | bytes | bytearray | None,
    expected_issuer: str | None = None,
    now: datetime | None = None,
    max_age_seconds: int = 120,
) -> dict[str, Any]:
    """Verify that an operator approval was signed by the pinned operator issuer."""
    return verify_envelope(decision, envelope, key=key, expected_issuer=expected_issuer,
                           now=now, max_age_seconds=max_age_seconds, domain=OPERATOR_DOMAIN)
