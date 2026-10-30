from datetime import datetime, timedelta, timezone

import pytest

from critical_consensus.evidence import (
    EvidenceError,
    bundle_body,
    make_envelope,
    payload_sha256,
    sign_bundle,
    verify_bundle,
    verify_envelope,
    verify_wrapper,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
KEY = "k" * 64
OTHER_KEY = "z" * 64
ISSUER = "test-ingress"


def test_valid_envelope_round_trip_binds_exact_payload():
    payload = {"b": [1, 2], "a": "value"}
    envelope = make_envelope(payload, key=KEY, issuer=ISSUER, issued_at=NOW, nonce="n" * 16)
    result = verify_envelope(payload, envelope, key=KEY, expected_issuer=ISSUER, now=NOW)
    assert result["valid"] is True
    assert result["payload_sha256"] == payload_sha256(payload)
    assert result["nonce"] == "n" * 16


def test_tampered_payload_or_signature_is_rejected():
    payload = {"a": 1}
    envelope = make_envelope(payload, key=KEY, issuer=ISSUER, issued_at=NOW)
    tampered = verify_envelope({"a": 2}, envelope, key=KEY, expected_issuer=ISSUER, now=NOW)
    assert tampered["valid"] is False
    assert "EVIDENCE_PAYLOAD_HASH_MISMATCH" in tampered["blockers"]

    forged = dict(envelope, signature="hmac-sha256:" + "0" * 64)
    assert "EVIDENCE_SIGNATURE_MISMATCH" in verify_envelope(
        payload, forged, key=KEY, expected_issuer=ISSUER, now=NOW)["blockers"]
    assert "EVIDENCE_SIGNATURE_MISMATCH" in verify_envelope(
        payload, envelope, key=OTHER_KEY, expected_issuer=ISSUER, now=NOW)["blockers"]


def test_issuer_expiry_and_future_timestamps_are_blocking():
    payload = {"a": 1}
    envelope = make_envelope(payload, key=KEY, issuer=ISSUER, issued_at=NOW, ttl_seconds=60)
    assert "EVIDENCE_ISSUER_MISMATCH" in verify_envelope(
        payload, envelope, key=KEY, expected_issuer="other", now=NOW)["blockers"]
    assert "EVIDENCE_EXPIRED" in verify_envelope(
        payload, envelope, key=KEY, expected_issuer=ISSUER,
        now=NOW + timedelta(seconds=61))["blockers"]
    future = make_envelope(payload, key=KEY, issuer=ISSUER, issued_at=NOW + timedelta(seconds=31))
    assert "EVIDENCE_ISSUED_AT_IN_FUTURE" in verify_envelope(
        payload, future, key=KEY, expected_issuer=ISSUER, now=NOW)["blockers"]


def test_short_keys_and_extra_envelope_fields_fail_closed():
    with pytest.raises(EvidenceError):
        make_envelope({"a": 1}, key="short", issuer=ISSUER)
    payload = {"a": 1}
    envelope = make_envelope(payload, key=KEY, issuer=ISSUER, issued_at=NOW)
    assert "EVIDENCE_KEY_NOT_CONFIGURED_OR_TOO_SHORT" in verify_envelope(
        payload, envelope, key=None, now=NOW)["blockers"]
    extended = dict(envelope, extra="field")
    assert "EVIDENCE_ENVELOPE_FIELDS_INVALID" in verify_envelope(
        payload, extended, key=KEY, now=NOW)["blockers"]


def test_non_canonical_payload_is_not_silently_stringified():
    with pytest.raises(EvidenceError):
        payload_sha256({"when": datetime.now(timezone.utc)})
    with pytest.raises(EvidenceError):
        payload_sha256({"value": float("nan")})


def test_bundle_signing_excludes_operator_decision_but_binds_everything_else():
    bundle = {"signal_id": "s1", "verdicts": []}
    signed = sign_bundle(bundle, key=KEY, issuer=ISSUER, issued_at=NOW)
    signed["user_decision"] = {"decision": "APPROVE"}
    assert verify_bundle(signed, key=KEY, expected_issuer=ISSUER, now=NOW)["valid"] is True
    assert "evidence_envelope" not in bundle_body(signed)
    assert "user_decision" not in bundle_body(signed)

    signed["verdicts"].append({"agent_id": "x"})
    assert "EVIDENCE_PAYLOAD_HASH_MISMATCH" in verify_bundle(
        signed, key=KEY, expected_issuer=ISSUER, now=NOW)["blockers"]


def test_wrapper_must_contain_exactly_payload_and_envelope():
    payload = {"a": 1}
    envelope = make_envelope(payload, key=KEY, issuer=ISSUER, issued_at=NOW)
    assert verify_wrapper({"payload": payload, "envelope": envelope}, key=KEY,
                          expected_issuer=ISSUER, now=NOW)["valid"] is True
    assert verify_wrapper({"payload": payload, "envelope": envelope, "extra": 1}, key=KEY,
                          now=NOW)["blockers"] == ["EVIDENCE_WRAPPER_INVALID"]
    assert verify_wrapper(None, key=KEY, now=NOW)["blockers"] == ["EVIDENCE_WRAPPER_INVALID"]
