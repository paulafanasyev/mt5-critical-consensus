"""Runs against the PostgreSQL service in validation-suite.yml; skipped locally without TEST_DATABASE_URL."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from critical_consensus.market_data import MarketBar
from critical_consensus.persistence import PostgresRepository


pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not configured")


def test_postgres_migrations_immutable_bars_and_idempotent_event():
    repo = PostgresRepository(os.environ["TEST_DATABASE_URL"])
    repo.migrate()
    symbol = "TEST" + uuid.uuid4().hex[:8].upper()
    stamp = datetime(2026, 10, 1, tzinfo=timezone.utc)
    bar = MarketBar(symbol, "H1", stamp, 1.0, 1.2, 0.9, 1.1, 100, 5)
    assert repo.append_market_bars([bar]) == {"inserted": 1, "identical_duplicates": 0, "total": 1}
    assert repo.append_market_bars([bar]) == {"inserted": 0, "identical_duplicates": 1, "total": 1}
    changed = MarketBar(symbol, "H1", stamp, 1.0, 1.25, 0.9, 1.1, 100, 5)
    with pytest.raises(ValueError, match="immutable"):
        repo.append_market_bars([changed])

    event = {"event_id": "test-" + uuid.uuid4().hex, "event_type": "TEST_EVENT",
             "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
             "payload": {"test": True}}
    assert repo.persist_event_once(event, consumer_name="pytest-integration") is True
    assert repo.persist_event_once(event, consumer_name="pytest-integration") is False
    tampered = dict(event, payload={"test": False})
    with pytest.raises(ValueError, match="different payload"):
        repo.persist_event_once(tampered, consumer_name="pytest-integration")


def test_postgres_forecast_cannot_be_rewritten_after_issue_or_resolution():
    repo = PostgresRepository(os.environ["TEST_DATABASE_URL"])
    repo.migrate()
    model_id = "test-" + uuid.uuid4().hex
    signal_id = "signal-" + uuid.uuid4().hex
    issued = datetime.now(timezone.utc) - timedelta(minutes=5)
    digest = "sha256:" + "c" * 64
    assert repo.record_forecast(model_id=model_id, signal_id=signal_id, issued_at=issued,
                                probability=0.73, input_sha256=digest) is True
    assert repo.record_forecast(model_id=model_id, signal_id=signal_id, issued_at=issued,
                                probability=0.73, input_sha256=digest) is False
    resolved = datetime.now(timezone.utc)
    assert repo.resolve_forecast(model_id=model_id, signal_id=signal_id, resolved_at=resolved, outcome=1) is True
    assert repo.resolve_forecast(model_id=model_id, signal_id=signal_id, resolved_at=resolved, outcome=1) is False
    with pytest.raises(ValueError, match="cannot be relabelled"):
        repo.resolve_forecast(model_id=model_id, signal_id=signal_id, resolved_at=resolved, outcome=0)
    exported = repo.export_resolved_forecasts(model_id=model_id)
    assert len(exported) == 1
    assert exported[0]["signal_id"] == signal_id
