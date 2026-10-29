from datetime import datetime, timedelta, timezone

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


def test_valid_read_only_snapshot_passes_and_reports_spread():
    result = validate_market_snapshot(make_snapshot(), now=NOW)
    assert result["valid"] is True
    assert result["blockers"] == []
    assert abs(result["spread"] - 0.0002) < 1e-12


def test_stale_source_quote_is_blocked():
    snapshot = make_snapshot(event_time=(NOW - timedelta(seconds=6)).isoformat())
    result = validate_market_snapshot(snapshot, now=NOW, max_age_seconds=5)
    assert result["valid"] is False
    assert "QUOTE_STALE" in result["blockers"]


def test_stale_local_capture_is_blocked():
    snapshot = make_snapshot(received_time=(NOW - timedelta(seconds=6)).isoformat())
    result = validate_market_snapshot(snapshot, now=NOW, max_age_seconds=5)
    assert result["valid"] is False
    assert "SNAPSHOT_STALE" in result["blockers"]


def test_missing_or_timezone_naive_timestamp_is_blocked():
    for key, value in (("event_time", None), ("received_time", "2026-10-29T12:00:00")):
        result = validate_market_snapshot(make_snapshot(**{key: value}), now=NOW)
        assert result["valid"] is False
        assert any(blocker.endswith("TIME_INVALID") for blocker in result["blockers"])


def test_crossed_quote_and_nonfinite_price_are_blocked():
    crossed = validate_market_snapshot(make_snapshot(bid=1.085, ask=1.084), now=NOW)
    nonfinite = validate_market_snapshot(make_snapshot(bid=float("nan")), now=NOW)
    assert "CROSSED_QUOTE" in crossed["blockers"]
    assert "BID_INVALID" in nonfinite["blockers"]


def test_boolean_is_not_accepted_as_a_price():
    result = validate_market_snapshot(make_snapshot(bid=True), now=NOW)
    assert result["valid"] is False
    assert "BID_INVALID" in result["blockers"]


def test_future_event_beyond_skew_is_blocked():
    snapshot = make_snapshot(event_time=(NOW + timedelta(seconds=3)).isoformat())
    result = validate_market_snapshot(snapshot, now=NOW, future_skew_seconds=2)
    assert result["valid"] is False
    assert "EVENT_TIME_IN_FUTURE" in result["blockers"]


def test_invalid_freshness_policy_fails_closed():
    result = validate_market_snapshot(make_snapshot(), now=NOW, max_age_seconds=0)
    assert result["valid"] is False
    assert "MAX_AGE_POLICY_INVALID" in result["blockers"]


def test_snapshot_does_not_authorize_or_create_an_order():
    result = validate_market_snapshot(make_snapshot(), now=NOW)
    assert "trade_allowed" not in result
    assert "order_command" not in result
