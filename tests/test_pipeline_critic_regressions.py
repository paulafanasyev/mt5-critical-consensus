from datetime import timedelta
import importlib.util
from pathlib import Path

from critical_consensus.audit import canonical_hash
from critical_consensus.pipeline import evaluate_signal_bundle

_spec = importlib.util.spec_from_file_location("pipeline_test_fixtures", Path(__file__).with_name("test_pipeline.py"))
assert _spec is not None and _spec.loader is not None
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
CERTAINTY_POLICY = _fixtures.CERTAINTY_POLICY
NOW = _fixtures.NOW
PERMISSION_MATRIX = _fixtures.PERMISSION_MATRIX
make_bundle = _fixtures.make_bundle


def _resign_chief(bundle):
    unsigned = dict(bundle["chief_reasoning"])
    unsigned.pop("reasoning_hash", None)
    bundle["chief_reasoning"]["reasoning_hash"] = canonical_hash(unsigned)
    bundle["permission_request"]["chief_reasoning_hash"] = bundle["chief_reasoning"]["reasoning_hash"]


def test_missing_chief_reasoning_hash_is_not_synthesized():
    bundle = make_bundle()
    bundle["chief_reasoning"].pop("reasoning_hash")
    bundle["permission_request"].pop("chief_reasoning_hash")
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "HOLD"
    assert "CHIEF_REASONING_HASH_MISSING" in result["blockers"]


def test_stale_chief_reasoning_is_rejected_even_with_valid_hash():
    bundle = make_bundle()
    bundle["chief_reasoning"]["created_at"] = (NOW - timedelta(seconds=301)).isoformat()
    _resign_chief(bundle)
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "HOLD"
    assert "CHIEF_REASONING_EXPIRED" in result["blockers"]


def test_resolved_challenge_requires_explicit_nonempty_string_evidence_refs():
    for bad_refs in (None, [], [None], [""], [0], "proof"):
        bundle = make_bundle()
        if bad_refs is None:
            bundle["challenge_records"][0].pop("resolution_evidence_refs")
        else:
            bundle["challenge_records"][0]["resolution_evidence_refs"] = bad_refs
        bundle["chief_reasoning"]["challenge_ledger_snapshot"] = [dict(bundle["challenge_records"][0])]
        bundle["chief_reasoning"]["challenge_ledger_hash"] = canonical_hash(bundle["challenge_records"])
        _resign_chief(bundle)
        result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
        assert result["status"] == "GATHER_MORE"
        assert any("OPEN_CHALLENGES_PRESENT" in blocker for blocker in result["blockers"])


def test_invalid_top_level_challenge_status_is_not_normalized_to_resolved():
    bundle = make_bundle()
    bundle["challenge_resolution_status"] = 123
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert any("CHALLENGE_RESOLUTION_STATUS_INVALID" in blocker for blocker in result["blockers"])


def test_malformed_challenge_target_and_type_are_rejected():
    for field, value in (("target_verdict_id", 0), ("challenge_type", None)):
        bundle = make_bundle()
        bundle["challenge_records"][0][field] = value
        bundle["chief_reasoning"]["challenge_ledger_snapshot"] = [dict(bundle["challenge_records"][0])]
        bundle["chief_reasoning"]["challenge_ledger_hash"] = canonical_hash(bundle["challenge_records"])
        _resign_chief(bundle)
        result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
        assert result["status"] == "GATHER_MORE"
        assert any("CHALLENGE_" in blocker for blocker in result["blockers"])


def test_non_mapping_peer_review_is_not_silently_dropped():
    bundle = make_bundle()
    bundle["peer_reviews"].append("malformed-review")
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert any("PEER_REVIEW_SET_INVALID_OR_HAS_DROPPED_RECORDS" in blocker for blocker in result["blockers"])
