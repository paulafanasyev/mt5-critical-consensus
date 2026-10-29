from datetime import timedelta

from test_pipeline import CERTAINTY_POLICY, NOW, PERMISSION_MATRIX, make_bundle
from critical_consensus.audit import canonical_hash
from critical_consensus.pipeline import evaluate_signal_bundle


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


def test_resolved_challenge_requires_nonempty_string_evidence_refs():
    for bad_refs in ([None], [""], [0], "proof"):
        bundle = make_bundle()
        bundle["challenge_records"][0]["resolution_evidence_refs"] = bad_refs
        bundle["chief_reasoning"]["challenge_ledger_snapshot"] = [dict(bundle["challenge_records"][0])]
        bundle["chief_reasoning"]["challenge_ledger_hash"] = canonical_hash(bundle["challenge_records"])
        _resign_chief(bundle)
        result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
        assert result["status"] == "GATHER_MORE"
        assert ("CHALLENGE_RESOLUTION_MISSING_OR_INVALID_EVIDENCE" in result["blockers"]
                or "OPEN_CHALLENGES_PRESENT" in result["blockers"])


def test_non_mapping_peer_review_is_not_silently_dropped():
    bundle = make_bundle()
    bundle["peer_reviews"].append("malformed-review")
    result = evaluate_signal_bundle(bundle, CERTAINTY_POLICY, PERMISSION_MATRIX, now=NOW)
    assert result["status"] == "GATHER_MORE"
    assert "PEER_REVIEW_SET_INVALID_OR_HAS_DROPPED_RECORDS" in result["blockers"]
