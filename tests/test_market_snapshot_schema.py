from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from critical_consensus.market_data import validate_market_snapshot

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas" / "market_snapshot_v1.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def valid_snapshot():
    return {
        "schema_version": 1,
        "snapshot_id": "snap-1",
        "source_id": "licensed-feed-a",
        "symbol": "EURUSD",
        "bid": 1.0840,
        "ask": 1.0842,
        "event_time": (NOW - timedelta(seconds=1)).isoformat(),
        "received_time": (NOW - timedelta(milliseconds=500)).isoformat(),
    }


def test_schema_and_runtime_agree_on_valid_snapshot():
    snapshot = valid_snapshot()
    assert not list(VALIDATOR.iter_errors(snapshot))
    result = validate_market_snapshot(
        snapshot, now=NOW, max_age_seconds=5, future_skew_seconds=2
    )
    assert result["valid"] is True


def test_schema_and_runtime_both_reject_whitespace_only_identifiers():
    for field in ("snapshot_id", "source_id"):
        snapshot = valid_snapshot()
        snapshot[field] = " \t "
        assert list(VALIDATOR.iter_errors(snapshot))
        result = validate_market_snapshot(
            snapshot, now=NOW, max_age_seconds=5, future_skew_seconds=2
        )
        assert result["valid"] is False
        assert f"{field.upper()}_MISSING" in result["blockers"]


def test_schema_is_structural_and_runtime_owns_semantic_quote_checks():
    crossed = valid_snapshot()
    crossed["bid"], crossed["ask"] = crossed["ask"], crossed["bid"]
    assert not list(VALIDATOR.iter_errors(crossed))
    result = validate_market_snapshot(
        crossed, now=NOW, max_age_seconds=5, future_skew_seconds=2
    )
    assert result["valid"] is False
    assert "CROSSED_QUOTE" in result["blockers"]


def test_schema_and_runtime_reject_trailing_line_terminators():
    for field, bad_value, blocker in (
        ("symbol", "EURUSD\n", "SYMBOL_INVALID"),
        ("event_time", valid_snapshot()["event_time"] + "\n", "EVENT_TIME_INVALID"),
        ("received_time", valid_snapshot()["received_time"] + "\n", "RECEIVED_TIME_INVALID"),
    ):
        snapshot = valid_snapshot()
        snapshot[field] = bad_value
        assert list(VALIDATOR.iter_errors(snapshot))
        result = validate_market_snapshot(
            snapshot, now=NOW, max_age_seconds=5, future_skew_seconds=2
        )
        assert result["valid"] is False
        assert blocker in result["blockers"]


def test_runtime_rejects_non_datetime_now_and_boolean_freshness_policies():
    snapshot = valid_snapshot()
    bad_clock = validate_market_snapshot(
        snapshot, now="2026-10-29T12:00:00Z", max_age_seconds=5, future_skew_seconds=2
    )
    bad_age = validate_market_snapshot(
        snapshot, now=NOW, max_age_seconds=True, future_skew_seconds=2
    )
    bad_skew = validate_market_snapshot(
        snapshot, now=NOW, max_age_seconds=5, future_skew_seconds=False
    )
    assert bad_clock["valid"] is False
    assert "CURRENT_TIME_INVALID" in bad_clock["blockers"]
    assert "MAX_AGE_POLICY_INVALID" in bad_age["blockers"]
    assert "FUTURE_SKEW_POLICY_INVALID" in bad_skew["blockers"]


def test_schema_version_numeric_integer_form_is_accepted_consistently():
    snapshot = valid_snapshot()
    snapshot["schema_version"] = 1.0
    assert not list(VALIDATOR.iter_errors(snapshot))
    result = validate_market_snapshot(
        snapshot, now=NOW, max_age_seconds=5, future_skew_seconds=2
    )
    assert result["valid"] is True
