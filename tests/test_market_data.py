from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from critical_consensus.market_data import validate_market_snapshot

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def make_snapshot(**overrides):
    snapshot = {
        "schema_version": 1,
        "snapshot_id": "snap-1",
        "source_id": "licensed-feed-a",
        "symbol": "EURUSD",
        "bid": 1.0840,
        "ask": 1.0842,
        "event_time": (NOW - timedelta(seconds=1)).isoformat(),
        "received_time": (NOW - timedelta(milliseconds=500)).isoformat(),
    }
    snapshot.update(overrides)
    return snapshot


def validate(snapshot, *, now=NOW, max_age_seconds=5, future_skew_seconds=2):
    return validate_market_snapshot(
        snapshot,
        now=now,
        max_age_seconds=max_age_seconds,
        future_skew_seconds=future_skew_seconds,
    )


def test_valid_read_only_snapshot_passes_and_reports_spread():
    result = validate(make_snapshot())
    assert result["valid"] is True
    assert result["blockers"] == []
    assert abs(result["spread"] - 0.0002) < 1e-12


def test_stale_source_quote_is_blocked():
    snapshot = make_snapshot(event_time=(NOW - timedelta(seconds=6)).isoformat())
    result = validate(snapshot, max_age_seconds=5)
    assert result["valid"] is False
    assert "QUOTE_STALE" in result["blockers"]


def test_stale_local_capture_is_blocked():
    snapshot = make_snapshot(received_time=(NOW - timedelta(seconds=6)).isoformat())
    result = validate(snapshot, max_age_seconds=5)
    assert result["valid"] is False
    assert "SNAPSHOT_STALE" in result["blockers"]


def test_missing_or_timezone_naive_timestamp_is_blocked():
    for key, value in (("event_time", None), ("received_time", "2026-10-29T12:00:00")):
        result = validate(make_snapshot(**{key: value}))
        assert result["valid"] is False
        assert any(blocker.endswith("TIME_INVALID") for blocker in result["blockers"])


def test_crossed_quote_and_nonfinite_price_are_blocked():
    crossed = validate(make_snapshot(bid=1.085, ask=1.084))
    nonfinite = validate(make_snapshot(bid=float("nan")))
    huge_integer = validate(make_snapshot(bid=10**400))
    assert "CROSSED_QUOTE" in crossed["blockers"]
    assert "BID_INVALID" in nonfinite["blockers"]
    assert "BID_INVALID" in huge_integer["blockers"]
    assert huge_integer["valid"] is False


def test_boolean_is_not_accepted_as_a_price():
    result = validate(make_snapshot(bid=True))
    assert result["valid"] is False
    assert "BID_INVALID" in result["blockers"]


def test_future_event_beyond_skew_is_blocked():
    snapshot = make_snapshot(event_time=(NOW + timedelta(seconds=3)).isoformat())
    result = validate(snapshot, future_skew_seconds=2)
    assert result["valid"] is False
    assert "EVENT_TIME_IN_FUTURE" in result["blockers"]


def test_future_time_within_skew_has_nonnegative_age_and_is_reported():
    snapshot = make_snapshot(event_time=(NOW + timedelta(seconds=1)).isoformat())
    result = validate(snapshot)
    assert result["valid"] is True
    assert result["age_seconds"] == 0
    assert result["event_clock_skew_seconds"] == 1


def test_missing_freshness_policy_fails_closed():
    result = validate_market_snapshot(make_snapshot(), now=NOW)
    assert result["valid"] is False
    assert "MAX_AGE_POLICY_INVALID" in result["blockers"]
    assert "FUTURE_SKEW_POLICY_INVALID" in result["blockers"]


def test_freshness_policy_upper_bounds_fail_closed():
    too_old = validate(make_snapshot(), max_age_seconds=61)
    too_much_skew = validate(make_snapshot(), future_skew_seconds=3)
    assert "MAX_AGE_POLICY_INVALID" in too_old["blockers"]
    assert "FUTURE_SKEW_POLICY_INVALID" in too_much_skew["blockers"]


def test_unknown_order_like_fields_are_rejected():
    result = validate(make_snapshot(trade_allowed=True, order_command={"side": "BUY"}))
    assert result["valid"] is False
    assert "UNKNOWN_FIELDS" in result["blockers"]


def test_timezone_extreme_that_overflows_utc_conversion_is_rejected():
    snapshot = make_snapshot(event_time="0001-01-01T00:00:00+23:59")
    result = validate(snapshot)
    assert result["valid"] is False
    assert "EVENT_TIME_INVALID" in result["blockers"]


def test_snapshot_schema_rejects_unknown_fields_and_bad_datetime_format():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schemas" / "market_snapshot_v1.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert not list(validator.iter_errors(make_snapshot()))
    assert list(validator.iter_errors(make_snapshot(extra="not allowed")))
    assert list(validator.iter_errors(make_snapshot(event_time="not-a-date")))


def test_invalid_freshness_policy_fails_closed():
    result = validate(make_snapshot(), max_age_seconds=0)
    assert result["valid"] is False
    assert "MAX_AGE_POLICY_INVALID" in result["blockers"]


def test_snapshot_does_not_authorize_or_create_an_order():
    result = validate(make_snapshot())
    assert "trade_allowed" not in result
    assert "order_command" not in result
