from datetime import datetime, timedelta, timezone

from critical_consensus.evidence import make_envelope
from critical_consensus.governance import (
    apply_attested_calibration,
    sign_attestation,
    verify_governance_attestation,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
KEY = "v" * 64
ISSUER = "calibration-service"
AGENT_CALIBRATION = {
    "PlatformScoutAgent": {"brier_score": 0.05, "sample_size": 100, "calibration_status": "CALIBRATED"},
    "TechnicalAnalystAgent": {"brier_score": 0.06, "sample_size": 120, "calibration_status": "CALIBRATED"},
}


def attestation(**overrides):
    payload = {
        "source": "calibration-service",
        "observed_at": NOW.isoformat(),
        "window_start": (NOW - timedelta(days=7)).isoformat(),
        "window_end": (NOW - timedelta(minutes=5)).isoformat(),
        "dissent_rate": 0.15, "dissent_sample_size": 100,
        "false_certainty_rate": 0.02, "false_certainty_sample_size": 100,
        "method_version": "brier-shrink-v1",
        "agent_calibration": AGENT_CALIBRATION,
    }
    payload.update(overrides)
    return sign_attestation(payload, key=KEY, issuer=ISSUER, issued_at=NOW)


def verify(wrapper, *, key=KEY, **kwargs):
    return verify_governance_attestation(wrapper, key=key, expected_issuer=ISSUER, now=NOW, **kwargs)


def test_valid_attestation_exposes_verified_metrics():
    result = verify(attestation())
    assert result["valid"] is True
    assert result["dissent_rate"] == 0.15
    assert result["false_certainty_sample_size"] == 100
    assert result["nonce"]


def test_wrong_signing_key_or_issuer_is_rejected():
    assert verify(attestation(), key="x" * 64)["valid"] is False
    wrong_issuer = attestation()
    wrong_issuer["envelope"] = make_envelope(wrong_issuer["payload"], key=KEY,
                                             issuer="rogue-service", issued_at=NOW)
    assert "GOVERNANCE_EVIDENCE_ISSUER_MISMATCH" in verify(wrong_issuer)["blockers"]


def test_stale_attestation_and_invalid_window_are_rejected():
    stale = attestation(observed_at=(NOW - timedelta(days=2)).isoformat())
    assert "GOVERNANCE_ATTESTATION_STALE" in verify(stale)["blockers"]
    reversed_window = attestation(window_start=(NOW - timedelta(days=1)).isoformat(),
                                  window_end=(NOW - timedelta(days=2)).isoformat())
    assert "GOVERNANCE_WINDOW_INVALID" in verify(reversed_window)["blockers"]
    long_window = attestation(window_start=(NOW - timedelta(days=40)).isoformat())
    assert "GOVERNANCE_WINDOW_TOO_LONG" in verify(long_window)["blockers"]


def test_boolean_and_string_metrics_are_not_coerced():
    assert "GOVERNANCE_DISSENT_RATE_INVALID" in verify(
        attestation(dissent_rate=True))["blockers"]
    assert "GOVERNANCE_FALSE_CERTAINTY_RATE_INVALID" in verify(
        attestation(false_certainty_rate="0.02"))["blockers"]
    assert "GOVERNANCE_DISSENT_SAMPLE_SIZE_INVALID" in verify(
        attestation(dissent_sample_size=10.5))["blockers"]


def test_missing_fields_and_unsigned_wrapper_fail_closed():
    wrapper = attestation()
    wrapper["payload"].pop("method_version")
    wrapper["envelope"] = sign_attestation(wrapper["payload"], key=KEY, issuer=ISSUER,
                                           issued_at=NOW)["envelope"]
    result = verify(wrapper)
    assert result["valid"] is False
    assert any(blocker.startswith("GOVERNANCE_FIELDS_MISSING") for blocker in result["blockers"])
    assert verify({"payload": {}})["valid"] is False


def test_evidence_domain_signature_cannot_masquerade_as_governance():
    payload = attestation()["payload"]
    cross_domain = {"payload": payload,
                    "envelope": make_envelope(payload, key=KEY, issuer=ISSUER, issued_at=NOW)}
    result = verify(cross_domain)
    assert result["valid"] is False
    assert "GOVERNANCE_EVIDENCE_SIGNATURE_MISMATCH" in result["blockers"]


def test_agent_calibration_report_is_required_and_validated():
    missing = attestation(agent_calibration={})
    assert "GOVERNANCE_AGENT_CALIBRATION_MISSING_OR_INVALID" in verify(missing)["blockers"]
    bad_status = attestation(agent_calibration={
        "PlatformScoutAgent": {"brier_score": 0.05, "sample_size": 100, "calibration_status": "UNVERIFIED"}})
    assert "GOVERNANCE_AGENT_CALIBRATION_NOT_CALIBRATED:PlatformScoutAgent" in verify(bad_status)["blockers"]
    bad_types = attestation(agent_calibration={
        "PlatformScoutAgent": {"brier_score": True, "sample_size": 100, "calibration_status": "CALIBRATED"}})
    assert "GOVERNANCE_AGENT_CALIBRATION_INVALID:PlatformScoutAgent" in verify(bad_types)["blockers"]


def test_attested_calibration_overwrites_self_declared_values():
    result = verify(attestation())
    verdicts = [
        {"agent_id": "PlatformScoutAgent", "calibration_meta": {
            "brier_score": 0.0, "sample_size": 10**6, "calibration_status": "CALIBRATED"}},
        {"agent_id": "UnknownAgent", "calibration_meta": {
            "brier_score": 0.01, "sample_size": 10**6, "calibration_status": "CALIBRATED"}},
    ]
    output = apply_attested_calibration(verdicts, result, issuer=ISSUER)
    assert output[0]["calibration_meta"]["brier_score"] == 0.05
    assert output[0]["calibration_meta"]["sample_size"] == 100
    assert output[0]["calibration_meta"]["source"] == ISSUER
    assert output[1]["calibration_meta"] is None
