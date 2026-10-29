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


def _resign_ledger_and_chief(bundle):
    bundle["chief_reasoning"]["challenge_ledger_snapshot"] = [dict(c) for c in bundle["challenge_records"]]
    bundle["chief_reasoning"]["challenge_ledger_hash"] = canonical_hash(bundle["challenge_records"])
    _resign_chief(bundle)


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
    assert "CHIEF_REASONING_SCHEMA_INVALID" in result["blockers"] or "CHIEF_REASONING_STALE_OR_FUTURE" in result["blockers"]


def test_resolved_challenge_requires_explicit_nonempty_string_evidence_refs():
    for bad_refs in (None, [], [None], [""], [0], "proof"):
        bundle = make_bundle()
        if bad_refs is None:
            bundle["challenge_records"][0].pop("resolution_evidence_refs")
            bundle["challenge_records"][0].pop("resolved_at")
        else:
            bundle["challenge_records"][0]["resolution_evidence_refs"] = bad_refs
        _resign_ledger_and_chief(bundle)
        result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
        assert result["status"] == "GATHER_MORE"
        assert "CHALLENGE" in " ".join(result["blockers"])


def test_invalid_top_level_challenge_status_is_not_normalized_to_resolved():
    bundle = make_bundle()
    bundle["challenge_resolution_status"] = 123
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert any("CHALLENGE_RESOLUTION_STATUS_INVALID" in blocker for blocker in result["blockers"])


def test_malformed_challenge_target_and_type_are_rejected():
    for field, value in (("target_verdict_id", 0), ("challenge_type", None),
                         ("challenge_type", "MADE_UP_TYPE")):
        bundle = make_bundle()
        bundle["challenge_records"][0][field] = value
        _resign_ledger_and_chief(bundle)
        result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
        assert result["status"] == "GATHER_MORE"
        assert any("CHALLENGE_" in blocker for blocker in result["blockers"])


def test_challenge_missing_required_fields_or_old_resolution_is_rejected():
    for mutation in ("missing_fields", "stale_resolution"):
        bundle = make_bundle()
        record = bundle["challenge_records"][0]
        if mutation == "missing_fields":
            for field in ("argument", "rounds", "created_at", "evidence_refs"):
                record.pop(field, None)
        else:
            record["resolved_at"] = (NOW - timedelta(seconds=301)).isoformat()
        _resign_ledger_and_chief(bundle)
        result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
        assert result["status"] == "GATHER_MORE"
        assert any("CHALLENGE" in blocker for blocker in result["blockers"])


def test_peer_review_missing_or_stale_or_future_contract_is_rejected():
    bundle = make_bundle()
    bundle["peer_reviews"][0].pop("review_id")
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert "INSUFFICIENT_OR_INVALID_PEER_REVIEWS" in result["blockers"]

    for timestamp in ((NOW - timedelta(seconds=301)).isoformat(), (NOW + timedelta(seconds=31)).isoformat()):
        bundle = make_bundle()
        bundle["peer_reviews"][0]["created_at"] = timestamp
        result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
        assert result["status"] == "GATHER_MORE"
        assert "INSUFFICIENT_OR_INVALID_PEER_REVIEWS" in result["blockers"]


def test_duplicate_review_ids_and_duplicate_source_ids_block_pipeline():
    bundle = make_bundle()
    bundle["peer_reviews"][1]["review_id"] = bundle["peer_reviews"][0]["review_id"]
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert "INSUFFICIENT_OR_INVALID_PEER_REVIEWS" in result["blockers"]

    bundle = make_bundle()
    bundle["key_sources"].append(dict(bundle["key_sources"][0]))
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert any("KEY_SOURCE_IDS_INVALID_OR_DUPLICATE" in blocker for blocker in result["blockers"])


def test_verdict_schema_and_server_owned_ttl_are_enforced():
    bundle = make_bundle()
    bundle["verdicts"][0]["reasoning"] = ""
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert any("VERDICT_0:VERDICT_SCHEMA_INVALID" in blocker for blocker in result["blockers"])

    bundle = make_bundle()
    bundle["verdicts"][0]["ttl_seconds"] = 3600
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert "VERDICT_0:VERDICT_TTL_INVALID" in result["blockers"]


def test_user_decision_requires_schema_complete_one_time_metadata():
    bundle = make_bundle()
    bundle["user_decision"] = {"permission_id": "perm-1", "user_id": "operator-1", "decision": "APPROVE",
        "timestamp": NOW.isoformat(), "reason": "reviewed", "ttl_seconds": 300}
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "DENY"
    assert "USER_DECISION_SCHEMA_INVALID" in result["blockers"]


def test_non_mapping_peer_review_is_not_silently_dropped():
    bundle = make_bundle()
    bundle["peer_reviews"].append("malformed-review")
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert "PEER_REVIEW_SET_INVALID_OR_HAS_DROPPED_RECORDS" in result["blockers"]
