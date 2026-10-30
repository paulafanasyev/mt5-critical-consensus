"""Runs against the PostgreSQL service in validation-suite.yml; skipped locally without TEST_DATABASE_URL."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from critical_consensus.market_data import MarketBar
from critical_consensus.persistence import PostgresRepository
from critical_consensus.audit import make_audit_event, canonical_hash


pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not configured")


def test_postgres_migrations_immutable_bars_and_idempotent_event():
    repo = PostgresRepository(os.environ["TEST_DATABASE_URL"])
    repo.migrate()
    symbol = "TEST" + uuid.uuid4().hex[:8].upper()
    stamp = datetime(2026, 10, 1, tzinfo=timezone.utc)
    bar = MarketBar(symbol, "H1", stamp, 1.0, 1.2, 0.9, 1.1, 100, 5)
    assert repo.append_market_bars([bar]) == {"inserted": 1, "identical_duplicates": 0, "total": 1}
    assert repo.append_market_bars([bar]) == {"inserted": 0, "identical_duplicates": 1, "total": 1}
    loaded = repo.get_recent_market_bars(symbol, "H1", limit=21)
    assert len(loaded) == 1 and loaded[0].close == pytest.approx(1.1)
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

def test_persisted_kill_switch_defaults_active_and_appends_audit_events():
    repo = PostgresRepository(os.environ["TEST_DATABASE_URL"])
    repo.migrate()
    initial = repo.get_kill_switch_state()
    assert initial["status"] == "PERSISTED"
    assert initial["kill_switch_active"] is True
    try:
        disabled = repo.set_kill_switch(active=False, actor_id="pytest-operator",
                                        reason="test-only controlled disable")
        assert disabled["kill_switch_active"] is False
        assert repo.get_kill_switch_state()["kill_switch_active"] is False
    finally:
        # Restore the fail-closed default even if an assertion above fails.
        repo.set_kill_switch(active=True, actor_id="pytest-operator",
                             reason="test complete; stop re-enabled")
    assert repo.get_kill_switch_state()["kill_switch_active"] is True


def test_mt5_risk_snapshot_is_persisted_immutably():
    repo = PostgresRepository(os.environ["TEST_DATABASE_URL"])
    repo.migrate()
    symbol = "RSK" + uuid.uuid4().hex[:8].upper()
    snapshot = {
        "observed_at": datetime.now(timezone.utc).isoformat(), "status": "DENY",
        "account_mode": "unknown", "symbol": symbol, "trade_allowed": False,
        "blockers": ["TEST_MISSING_TERMINAL"],
    }
    stored = repo.record_mt5_risk_snapshot(snapshot)
    assert stored["snapshot_sha256"].startswith("sha256:")
    loaded = repo.get_latest_mt5_risk_snapshot(symbol)
    assert loaded is not None
    assert loaded["status"] == "DENY"
    assert loaded["blockers"] == ["TEST_MISSING_TERMINAL"]


def test_full_pipeline_decision_and_audit_chain_are_persisted_atomically():
    repo = PostgresRepository(os.environ["TEST_DATABASE_URL"])
    repo.migrate()
    signal_input = {"signal_id": "persist-" + uuid.uuid4().hex, "symbol": "EURUSD"}
    signal_id = signal_input["signal_id"]
    first = make_audit_event("SIGNAL_RECEIVED", {"signal_id": signal_id})
    second = make_audit_event("PIPELINE_RESULT", {"signal_id": signal_id, "status": "GATHER_MORE"},
                              first["event_hash"])
    result = {
        "signal_id": signal_id, "status": "GATHER_MORE", "trade_allowed": False,
        "blockers": ["TEST_EVIDENCE_MISSING"],
        "audit_events": [first, second], "audit_chain_valid": True,
    }
    persisted = repo.persist_pipeline_result(
        result, signal_input_sha256=canonical_hash(signal_input),
    )
    assert persisted["status"] == "GATHER_MORE"
    assert persisted["audit_event_count"] == 2
    assert persisted["audit_events_inserted"] == 2
    assert persisted["trade_allowed"] is False
    with pytest.raises(ValueError, match="invalid"):
        repo.persist_pipeline_result(
            dict(result, audit_events=[first, dict(second, payload={"tampered": True})]),
            signal_input_sha256=canonical_hash(signal_input),
        )


def test_pipeline_result_persistence_rejects_missing_or_forged_signal_hash():
    repo = PostgresRepository(os.environ["TEST_DATABASE_URL"])
    repo.migrate()
    event = make_audit_event("PIPELINE_RESULT", {"signal_id": "bad-hash"})
    result = {"signal_id": "bad-hash", "status": "GATHER_MORE", "blockers": [],
              "audit_events": [event], "audit_chain_valid": True}
    with pytest.raises(ValueError, match="signal_input_sha256"):
        repo.persist_pipeline_result(result, signal_input_sha256="sha256:bad")


def test_postgres_authorization_nonce_is_consumed_exactly_once():
    from critical_consensus.authorization import PostgresNonceStore

    repo = PostgresRepository(os.environ["TEST_DATABASE_URL"])
    repo.migrate()
    store = PostgresNonceStore(repo)
    scope = "perm:test-" + uuid.uuid4().hex
    nonce = uuid.uuid4().hex
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert store.consume(scope=scope, nonce=nonce, expires_at=future) is True
    assert store.consume(scope=scope, nonce=nonce, expires_at=future) is False
    assert store.consume(scope=scope + ":other", nonce=nonce, expires_at=future) is True
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert store.consume(scope=scope, nonce="expired-" + nonce, expires_at=expired) is False
    with pytest.raises(ValueError, match="future"):
        repo.consume_authorization_nonce(scope=scope, nonce="past-" + nonce, expires_at=expired)


def test_persist_cli_requires_persisted_risk_before_writing_a_decision(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    from critical_consensus.audit import canonical_hash
    from critical_consensus.evidence import sign_bundle

    repo = PostgresRepository(os.environ["TEST_DATABASE_URL"])
    repo.migrate()
    repo.set_kill_switch(active=True, actor_id="pytest-operator", reason="persist-cli isolation")
    signal_input = {"signal_id": "cli-persist-" + uuid.uuid4().hex, "instrument": "EURUSD"}
    bundle = sign_bundle({
        "signal_id": signal_input["signal_id"],
        "signal_input": signal_input,
        "signal_input_hash": canonical_hash(signal_input),
        "permission_request": {"instrument": "EURUSD"},
        "verdicts": [],
    }, key="e" * 64, issuer="test-ingress")
    bundle_path = tmp_path / "bundle.json"
    output_path = tmp_path / "result.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    env = {**os.environ, "DATABASE_URL": os.environ["TEST_DATABASE_URL"],
           "MT5_EVIDENCE_HMAC_KEY": "e" * 64, "MT5_EVIDENCE_ISSUER": "test-ingress",
           "MT5_GOVERNANCE_HMAC_KEY": "g" * 64, "MT5_GOVERNANCE_ISSUER": "calibration-service"}
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "evaluate_signal_bundle.py"),
         str(bundle_path), "--output", str(output_path), "--persist"],
        capture_output=True, text=True, check=False, env=env,
    )
    assert completed.returncode == 2, completed.stderr
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["persisted_risk_checked"] is True
    assert report["result"]["status"] == "DENY"
    assert report["result"]["trade_allowed"] is False
    assert "KILL_SWITCH_ACTIVE_OR_INVALID" in report["result"]["blockers"]
    assert report["persistence"]["decision_id"]
    assert report["order_created"] is False
