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
