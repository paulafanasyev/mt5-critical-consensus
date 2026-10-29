from datetime import timedelta
import importlib.util
from pathlib import Path

from critical_consensus.audit import canonical_hash
from critical_consensus import chief_skeptic_pass, evaluate_signal_bundle

_spec = importlib.util.spec_from_file_location("pipeline_test_fixtures", Path(__file__).with_name("test_pipeline.py"))
assert _spec is not None and _spec.loader is not None
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)


def _resign(reasoning):
    unsigned = dict(reasoning)
    unsigned.pop("reasoning_hash", None)
    reasoning["reasoning_hash"] = canonical_hash(unsigned)


def test_package_level_chief_gate_rejects_stale_and_schema_invalid_reasoning():
    bundle = _fixtures.make_bundle()
    chief = bundle["chief_reasoning"]
    stale = dict(chief)
    stale["created_at"] = (_fixtures.NOW - timedelta(seconds=301)).isoformat()
    _resign(stale)
    result = chief_skeptic_pass(stale, now=_fixtures.NOW)
    assert result["verdict"] == "HOLD"
    assert "CHIEF_REASONING_EXPIRED" in result["blockers"]

    malformed = dict(chief)
    malformed["steps"] = {key: dict(value) for key, value in chief["steps"].items()}
    malformed["steps"]["a"]["evidence_refs"] = [0]
    _resign(malformed)
    result = chief_skeptic_pass(malformed, now=_fixtures.NOW)
    assert result["verdict"] == "HOLD"
    assert "CHIEF_REASONING_SCHEMA_INVALID" in result["blockers"]


def test_package_level_pipeline_fails_closed_on_unhashable_enum_value():
    bundle = _fixtures.make_bundle()
    bundle["challenge_records"][0]["status"] = []
    result = evaluate_signal_bundle(bundle, _fixtures.CERTAINTY_POLICY, _fixtures.PERMISSION_MATRIX, now=_fixtures.NOW)
    assert result["trade_allowed"] is False
    assert result["status"] != "AUTHORIZED_ONCE"
