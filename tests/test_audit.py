from critical_consensus.audit import make_audit_event, verify_audit_chain


def test_audit_chain_accepts_untampered_events():
    first = make_audit_event("SIGNAL_RECEIVED", {"signal_id": "s-1"})
    second = make_audit_event("GATE_FAILED", {"signal_id": "s-1", "reason": "OPEN_CHALLENGE"}, first["event_hash"])
    result = verify_audit_chain([first, second])
    assert result["valid"] is True
    assert result["events_verified"] == 2


def test_audit_chain_detects_tampering():
    first = make_audit_event("SIGNAL_RECEIVED", {"signal_id": "s-1"})
    second = make_audit_event("GATE_FAILED", {"signal_id": "s-1"}, first["event_hash"])
    second["payload"]["reason"] = "changed"
    result = verify_audit_chain([first, second])
    assert result["valid"] is False
    assert result["reason"] == "EVENT_CONTENT_HASH_MISMATCH"


def test_audit_chain_detects_broken_link():
    first = make_audit_event("SIGNAL_RECEIVED", {"signal_id": "s-1"})
    second = make_audit_event("GATE_FAILED", {"signal_id": "s-1"}, "sha256:bad")
    result = verify_audit_chain([first, second])
    assert result["valid"] is False
    assert result["reason"] == "PREVIOUS_HASH_MISMATCH"


def test_canonical_hash_rejects_nan_and_infinity():
    import pytest
    from critical_consensus.audit import canonical_hash
    with pytest.raises(ValueError):
        canonical_hash({"confidence": float("nan")})
    with pytest.raises(ValueError):
        canonical_hash({"confidence": float("inf")})


def test_audit_event_rejects_nonfinite_payload_before_signing():
    import pytest
    with pytest.raises(ValueError):
        make_audit_event("INVALID", {"probability": float("nan")})
