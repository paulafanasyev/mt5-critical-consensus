"""Optional PostgreSQL persistence for immutable market data and audit evidence.

Install with `pip install -e '.[data]'`. Every write commits before a Redis worker
acknowledges its message. This repository stores evidence; it does not execute orders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .market_data import MarketBar, validate_bar_series
from .audit import verify_audit_chain
from .source_provenance import validate_source_capture

ROOT = Path(__file__).resolve().parents[2]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False, default=str)


def _validate_event_json_value(value: Any, *, depth: int = 0) -> None:
    """Validate JSON-native inbox payloads without lossy default=str coercion."""
    if depth > 32:
        raise ValueError("event JSON nesting exceeds 32 levels")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        value.encode("utf-8")
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("event JSON numbers must be finite")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("event JSON object keys must be strings")
            key.encode("utf-8")
            _validate_event_json_value(item, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_event_json_value(item, depth=depth + 1)
        return
    raise ValueError("event must contain only JSON-native values")


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp(value: Any, name: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


class PostgresRepository:
    def __init__(self, dsn: str | None = None, *, connect_fn=None):
        self.dsn = dsn or os.getenv("DATABASE_URL", "")
        if not self.dsn and connect_fn is None:
            raise ValueError("DATABASE_URL or an explicit connect_fn is required")
        self._connect_fn = connect_fn

    def _connect(self):
        if self._connect_fn is not None:
            return self._connect_fn(self.dsn)
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("install the optional 'data' extra to use PostgreSQL persistence") from exc
        return psycopg.connect(self.dsn)

    def migrate(self) -> None:
        """Apply the repository's base schema and ordered SQL migrations transactionally."""
        base_schema = ROOT / "db" / "postgres_schema.sql"
        migrations = sorted((ROOT / "db" / "migrations").glob("*.sql"))
        if not base_schema.exists():
            raise RuntimeError(f"base database schema is missing: {base_schema}")
        scripts = [base_schema.read_text(encoding="utf-8")] + [
            path.read_text(encoding="utf-8") for path in migrations
        ]
        with self._connect() as conn:
            for script in scripts:
                conn.execute(script, prepare=False)

    def append_market_bars(self, bars: Iterable[MarketBar]) -> dict[str, int]:
        records = validate_bar_series(bars)
        inserted = duplicates = 0
        sql = """
          INSERT INTO market_bars
            (symbol, timeframe, bar_time, open, high, low, close, tick_volume, spread_points, source)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT (symbol, timeframe, bar_time) DO NOTHING
          RETURNING bar_time
        """
        with self._connect() as conn:
            for bar in records:
                args = (bar.symbol, bar.timeframe, bar.time, bar.open, bar.high, bar.low,
                        bar.close, bar.tick_volume, bar.spread_points, bar.source)
                returned = conn.execute(sql, args).fetchone()
                if returned:
                    inserted += 1
                    continue
                existing = conn.execute(
                    """SELECT open, high, low, close, tick_volume, spread_points, source
                       FROM market_bars WHERE symbol=%s AND timeframe=%s AND bar_time=%s""",
                    (bar.symbol, bar.timeframe, bar.time),
                ).fetchone()
                if existing is None:
                    raise RuntimeError("market-bar conflict row disappeared during ingestion")
                numeric_existing = existing[:4]
                numeric_new = (bar.open, bar.high, bar.low, bar.close)
                same_prices = all(
                    math.isclose(float(old), float(new), rel_tol=1e-12, abs_tol=1e-12)
                    for old, new in zip(numeric_existing, numeric_new)
                )
                if (not same_prices or existing[4] != bar.tick_volume
                        or existing[5] != bar.spread_points or existing[6] != bar.source):
                    raise ValueError("conflicting data for an existing immutable market bar")
                duplicates += 1
        return {"inserted": inserted, "identical_duplicates": duplicates, "total": len(records)}

    def get_recent_market_bars(self, symbol: str, timeframe: str, limit: int = 200) -> list[MarketBar]:
        """Return the newest immutable bars in chronological order."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 21 <= limit <= 5000:
            raise ValueError("limit must be an integer between 21 and 5000")
        symbol, timeframe = str(symbol).strip().upper(), str(timeframe).strip().upper()
        if not symbol or not timeframe:
            raise ValueError("symbol and timeframe are required")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT symbol, timeframe, bar_time, open, high, low, close, tick_volume, spread_points, source
                   FROM market_bars WHERE symbol=%s AND timeframe=%s ORDER BY bar_time DESC LIMIT %s""",
                (symbol, timeframe, limit),
            ).fetchall()
        return [
            MarketBar(row[0], row[1], row[2], float(row[3]), float(row[4]), float(row[5]), float(row[6]),
                      int(row[7]) if row[7] is not None else None,
                      int(row[8]) if row[8] is not None else None, str(row[9]))
            for row in reversed(rows)
        ]

    def record_source_evidence(self, record: Mapping[str, Any], *, registry: Mapping[str, Any]) -> dict[str, Any]:
        """Store a publisher capture and its exact bytes immutably; identical replays are idempotent."""
        if not isinstance(record, Mapping) or not isinstance(registry, Mapping):
            raise ValueError("source evidence record and registry must be objects")
        validation = validate_source_capture(record, registry)
        if validation.get("status") != "PASS":
            raise ValueError("source evidence failed publisher provenance policy: " +
                             ",".join(validation.get("blockers", [])))
        required = (
            "source_id", "publisher_id", "publisher_domain", "source_url", "published_at",
            "retrieved_at", "independence_group", "credibility", "ttl_seconds",
            "content_sha256", "content_bytes", "captured_content", "response_content_type",
            "publication_time_attestation", "publication_time_field", "capture_method", "registry_version",
        )
        if any(key not in record for key in required):
            raise ValueError("source evidence record is missing required fields")
        captured = record.get("captured_content")
        if not isinstance(captured, (bytes, bytearray, memoryview)) or not captured:
            raise ValueError("captured_content must be non-empty raw HTTPS response bytes")
        content = bytes(captured)
        digest = str(record.get("content_sha256", ""))
        if (len(digest) != 71 or not digest.startswith("sha256:")
                or hashlib.sha256(content).hexdigest() != digest[7:]):
            raise ValueError("captured source bytes do not match content_sha256")
        if len(content) != int(record.get("content_bytes", -1)) or len(content) > 512_000:
            raise ValueError("source content byte count is invalid")
        source_id = str(record.get("source_id", "")).strip()
        if not source_id.startswith("src-") or len(source_id) != 44:
            raise ValueError("source_id must be a deterministic src- identifier")
        published = _timestamp(record.get("published_at"), "published_at")
        retrieved = _timestamp(record.get("retrieved_at"), "retrieved_at")
        credibility = record.get("credibility")
        if isinstance(credibility, bool) or not math.isfinite(float(credibility)) or not 0 <= float(credibility) <= 1:
            raise ValueError("credibility must be finite and in [0,1]")
        ttl = record.get("ttl_seconds")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        response_content_type = str(record.get("response_content_type", "")).split(";", 1)[0].strip().lower()

        args = (
            source_id, str(record["publisher_id"]), str(record["publisher_domain"]),
            str(record["source_url"]), published, retrieved, str(record["independence_group"]),
            float(credibility), ttl, digest, len(content), content,
            str(record["publication_time_attestation"]), str(record["publication_time_field"]),
            str(record["capture_method"]), str(record["registry_version"]), response_content_type,
        )
        with self._connect() as conn:
            inserted = conn.execute(
                """INSERT INTO source_evidence
                   (source_id, publisher_id, publisher_domain, source_url, published_at, retrieved_at,
                    independence_group, credibility, ttl_seconds, content_sha256, content_bytes,
                    captured_content, publication_time_attestation, publication_time_field,
                    capture_method, registry_version, response_content_type)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (source_id) DO NOTHING RETURNING source_id""", args
            ).fetchone()
            if inserted:
                return {"source_id": source_id, "inserted": True,
                        "content_sha256": digest, "content_bytes": len(content)}
            existing = conn.execute(
                """SELECT publisher_id, publisher_domain, source_url, published_at, retrieved_at,
                          independence_group, credibility, ttl_seconds, content_sha256, content_bytes,
                          captured_content, publication_time_attestation, publication_time_field,
                          capture_method, registry_version, response_content_type
                   FROM source_evidence WHERE source_id=%s""", (source_id,)
            ).fetchone()
            if existing is None:
                raise RuntimeError("source evidence conflict row disappeared")
            actual = dict(zip(
                ("publisher_id", "publisher_domain", "source_url", "published_at", "retrieved_at",
                 "independence_group", "credibility", "ttl_seconds", "content_sha256", "content_bytes",
                 "captured_content", "publication_time_attestation", "publication_time_field",
                 "capture_method", "registry_version", "response_content_type"),
                existing,
            ))
            # Compare by column name to avoid positional drift. retrieved_at is intentionally
            # excluded: a later fetch of the same source bytes is an idempotent replay.
            equal = (
                actual["publisher_id"] == str(record["publisher_id"])
                and actual["publisher_domain"] == str(record["publisher_domain"])
                and actual["source_url"] == str(record["source_url"])
                and actual["published_at"] == published
                and actual["independence_group"] == str(record["independence_group"])
                and math.isclose(float(actual["credibility"]), float(credibility), rel_tol=0, abs_tol=1e-12)
                and int(actual["ttl_seconds"]) == ttl
                and actual["content_sha256"] == digest
                and int(actual["content_bytes"]) == len(content)
                and bytes(actual["captured_content"]) == content
                and actual["publication_time_attestation"] == str(record["publication_time_attestation"])
                and actual["publication_time_field"] == str(record["publication_time_field"])
                and actual["capture_method"] == str(record["capture_method"])
                and actual["registry_version"] == str(record["registry_version"])
                and actual["response_content_type"] == response_content_type
            )
            if not equal:
                raise ValueError("source_id already exists with different captured evidence")
        return {"source_id": source_id, "inserted": False,
                "content_sha256": digest, "content_bytes": len(content)}

    def get_source_evidence(self, source_ids: Sequence[str]) -> list[dict[str, Any]]:
        """Load immutable publisher captures by exact IDs; never substitutes a similar URL."""
        if (not isinstance(source_ids, Sequence) or isinstance(source_ids, (str, bytes))
                or any(not isinstance(item, str) or not item.strip() for item in source_ids)):
            raise ValueError("source_ids must be a sequence of non-empty strings")
        ids = [item.strip() for item in source_ids]
        if len(set(ids)) != len(ids):
            raise ValueError("source_ids must be unique")
        if not ids:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT source_id, publisher_id, publisher_domain, source_url, published_at,
                          retrieved_at, independence_group, credibility, ttl_seconds, content_sha256,
                          content_bytes, captured_content, publication_time_attestation,
                          publication_time_field, capture_method, registry_version, response_content_type
                   FROM source_evidence WHERE source_id = ANY(%s)""", (ids,)
            ).fetchall()
        columns = (
            "source_id", "publisher_id", "publisher_domain", "source_url", "published_at",
            "retrieved_at", "independence_group", "credibility", "ttl_seconds", "content_sha256",
            "content_bytes", "captured_content", "publication_time_attestation",
            "publication_time_field", "capture_method", "registry_version", "response_content_type",
        )
        by_id = {str(row[0]): dict(zip(columns, row)) for row in rows}
        return [by_id[item] for item in ids if item in by_id]

    def record_forecasts(self, forecasts: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        """Atomically persist raw model-confidence observations before any outcomes are known.

        A conflicting replay aborts the entire batch. These probabilities are raw model
        confidence observations, not calibrated probabilities or trading-performance claims.
        """
        if (not isinstance(forecasts, Sequence)
                or isinstance(forecasts, (str, bytes, bytearray))):
            raise ValueError("forecasts must be a sequence of objects")
        if not forecasts:
            return {"inserted": 0, "identical_replays": 0, "total": 0}

        prepared: list[tuple[str, str, datetime, float, str]] = []
        seen: set[tuple[str, str]] = set()
        for index, forecast in enumerate(forecasts):
            if not isinstance(forecast, Mapping):
                raise ValueError(f"forecast {index} must be an object")
            raw_model_id = forecast.get("model_id")
            raw_signal_id = forecast.get("signal_id")
            if not isinstance(raw_model_id, str) or not isinstance(raw_signal_id, str):
                raise ValueError(f"forecast {index} model_id and signal_id must be strings")
            model_id, signal_id = raw_model_id.strip(), raw_signal_id.strip()
            if not model_id or not signal_id:
                raise ValueError(f"forecast {index} requires model_id and signal_id")
            key = (model_id, signal_id)
            if key in seen:
                raise ValueError("forecast batch contains duplicate model_id/signal_id keys")
            seen.add(key)
            issued = _timestamp(forecast.get("issued_at"), "issued_at")
            raw_probability = forecast.get("probability")
            if isinstance(raw_probability, bool) or not isinstance(raw_probability, (int, float)):
                raise ValueError("probability must be a numeric value, not a string or boolean")
            try:
                probability = float(raw_probability)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("probability must be finite numeric data") from exc
            if not math.isfinite(probability) or not 0 <= probability <= 1:
                raise ValueError("probability must be finite and in [0,1]")
            input_sha256 = forecast.get("input_sha256")
            if (not isinstance(input_sha256, str) or len(input_sha256) != 71
                    or not input_sha256.startswith("sha256:")
                    or any(ch not in "0123456789abcdef" for ch in input_sha256[7:])):
                raise ValueError("input_sha256 must use sha256:<64 lowercase hex characters>")
            prepared.append((model_id, signal_id, issued, probability, input_sha256))

        inserted_count = replay_count = 0
        with self._connect() as conn:
            for model_id, signal_id, issued, probability, input_sha256 in prepared:
                inserted = conn.execute(
                    """INSERT INTO forecast_predictions(model_id, signal_id, issued_at, probability, input_sha256)
                       VALUES (%s,%s,%s,%s,%s) ON CONFLICT (model_id, signal_id) DO NOTHING
                       RETURNING signal_id""",
                    (model_id, signal_id, issued, probability, input_sha256),
                ).fetchone()
                if inserted:
                    inserted_count += 1
                    continue
                old = conn.execute(
                    """SELECT issued_at, probability, input_sha256 FROM forecast_predictions
                       WHERE model_id=%s AND signal_id=%s""", (model_id, signal_id)
                ).fetchone()
                if old is None:
                    raise RuntimeError("forecast conflict row disappeared")
                if (old[0] != issued
                        or not math.isclose(float(old[1]), probability, rel_tol=0.0, abs_tol=0.0)
                        or old[2] != input_sha256):
                    raise ValueError("conflicting replay for immutable issued forecast")
                replay_count += 1
        return {"inserted": inserted_count, "identical_replays": replay_count, "total": len(prepared)}

    def record_forecast(
        self, *, model_id: str, signal_id: str, issued_at: datetime | str,
        probability: float, input_sha256: str,
    ) -> bool:
        """Persist one raw confidence observation; False means an identical replay."""
        result = self.record_forecasts([{
            "model_id": model_id, "signal_id": signal_id, "issued_at": issued_at,
            "probability": probability, "input_sha256": input_sha256,
        }])
        return result["inserted"] == 1

    def resolve_forecast(self, *, model_id: str, signal_id: str, resolved_at: datetime | str, outcome: int) -> bool:
        """Label a prior forecast once the outcome is known; conflicting relabels fail closed."""
        if isinstance(outcome, bool) or outcome not in (0, 1):
            raise ValueError("outcome must be integer 0 or 1")
        resolved = _timestamp(resolved_at, "resolved_at")
        with self._connect() as conn:
            row = conn.execute(
                """SELECT issued_at, outcome, resolved_at FROM forecast_predictions
                   WHERE model_id=%s AND signal_id=%s FOR UPDATE""", (model_id, signal_id)
            ).fetchone()
            if row is None:
                raise ValueError("cannot resolve an unknown forecast")
            if resolved <= row[0]:
                raise ValueError("resolved_at must be later than issued_at")
            if row[1] is not None:
                if row[1] == outcome and row[2] == resolved:
                    return False
                raise ValueError("a resolved forecast cannot be relabelled")
            conn.execute(
                """UPDATE forecast_predictions SET outcome=%s, resolved_at=%s
                   WHERE model_id=%s AND signal_id=%s""", (int(outcome), resolved, model_id, signal_id)
            )
        return True

    def export_resolved_forecasts(self, *, model_id: str | None = None) -> list[dict[str, Any]]:
        query = """SELECT model_id, signal_id, issued_at, resolved_at, probability, outcome
                   FROM forecast_predictions WHERE outcome IS NOT NULL"""
        params: tuple[Any, ...] = ()
        if model_id:
            query += " AND model_id=%s"
            params = (model_id,)
        query += " ORDER BY issued_at, model_id, signal_id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [{
            "model_id": row[0], "signal_id": row[1], "issued_at": row[2].isoformat(),
            "resolved_at": row[3].isoformat(), "probability": float(row[4]), "outcome": int(row[5]),
        } for row in rows]

    def store_calibration_report(self, report: Mapping[str, Any]) -> dict[str, str | bool]:
        if not isinstance(report, Mapping) or not isinstance(report.get("dataset"), Mapping):
            raise ValueError("report must include dataset SHA-256 metadata")
        dataset_sha = report["dataset"].get("sha256")
        if not isinstance(dataset_sha, str) or len(dataset_sha) != 71 or not dataset_sha.startswith("sha256:"):
            raise ValueError("report dataset sha256 is required")
        report_json = _canonical(dict(report))
        report_hash = _sha256(report_json)
        with self._connect() as conn:
            inserted = conn.execute(
                """INSERT INTO calibration_reports(protocol_version, dataset_sha256, report_sha256, report)
                   VALUES (%s,%s,%s,%s::jsonb) ON CONFLICT (report_sha256) DO NOTHING
                   RETURNING report_id""",
                (str(report.get("protocol_version", "unknown")), dataset_sha, report_hash, report_json),
            ).fetchone()
            if inserted:
                return {"report_sha256": report_hash, "inserted": True}
            existing = conn.execute(
                "SELECT dataset_sha256 FROM calibration_reports WHERE report_sha256=%s", (report_hash,)
            ).fetchone()
            if not existing or existing[0] != dataset_sha:
                raise RuntimeError("calibration report hash conflict")
        return {"report_sha256": report_hash, "inserted": False}

    def get_kill_switch_state(self) -> dict[str, Any]:
        """Return persisted switch state; missing/unavailable state must be interpreted as active."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT kill_switch_active, updated_at, updated_by, reason
                   FROM risk_control_state WHERE singleton=TRUE"""
            ).fetchone()
        if row is None:
            return {"kill_switch_active": True, "status": "UNKNOWN", "updated_by": "",
                    "reason": "Missing risk-control row; fail closed", "updated_at": None}
        return {"kill_switch_active": bool(row[0]), "status": "PERSISTED",
                "updated_at": row[1].isoformat(), "updated_by": str(row[2]), "reason": str(row[3])}

    def set_kill_switch(self, *, active: bool, actor_id: str, reason: str) -> dict[str, Any]:
        """Persist emergency-stop state and append a hash-chained event in one transaction."""
        if not isinstance(active, bool):
            raise ValueError("active must be a boolean")
        actor = str(actor_id).strip()
        explanation = str(reason).strip()
        if not actor or len(actor) > 128 or not explanation or len(explanation) > 1000:
            raise ValueError("actor_id and a reason of at most 1000 characters are required")
        occurred = datetime.now(timezone.utc)
        with self._connect() as conn:
            state = conn.execute(
                """SELECT kill_switch_active FROM risk_control_state
                   WHERE singleton=TRUE FOR UPDATE"""
            ).fetchone()
            if state is None:
                # Still fail closed if migrations were skipped or the singleton was removed.
                raise RuntimeError("risk_control_state missing; apply migrations before changing kill switch")
            previous = conn.execute(
                "SELECT event_hash FROM risk_control_events ORDER BY event_seq DESC LIMIT 1"
            ).fetchone()
            previous_hash = str(previous[0]) if previous else None
            event = {"kill_switch_active": active, "actor_id": actor, "reason": explanation,
                     "occurred_at": occurred.isoformat(), "previous_hash": previous_hash}
            encoded = _canonical(event)
            event_hash = _sha256(encoded)
            conn.execute(
                """INSERT INTO risk_control_events
                   (kill_switch_active, actor_id, reason, occurred_at, previous_hash, event_hash, event)
                   VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (active, actor, explanation, occurred, previous_hash, event_hash, encoded),
            )
            conn.execute(
                """UPDATE risk_control_state SET kill_switch_active=%s, updated_at=%s,
                   updated_by=%s, reason=%s WHERE singleton=TRUE""",
                (active, occurred, actor, explanation),
            )
        return {"kill_switch_active": active, "status": "PERSISTED", "updated_at": occurred.isoformat(),
                "updated_by": actor, "reason": explanation, "event_hash": event_hash}

    def record_mt5_risk_snapshot(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Append an immutable read-only MT5 risk snapshot, including denied/partial captures."""
        if not isinstance(snapshot, Mapping):
            raise ValueError("snapshot must be an object")
        observed = _timestamp(snapshot.get("observed_at", ""), "observed_at")
        account_mode = str(snapshot.get("account_mode", "unknown")).lower()
        if account_mode not in {"demo", "contest", "real", "unknown"}:
            account_mode = "unknown"
        symbol = str(snapshot.get("symbol", "")).strip().upper() or "UNKNOWN"
        encoded = _canonical(dict(snapshot))
        digest = _sha256(encoded)
        with self._connect() as conn:
            row = conn.execute(
                """INSERT INTO mt5_risk_snapshots
                   (observed_at, terminal_server, account_mode, symbol, snapshot_sha256, snapshot)
                   VALUES (%s,%s,%s,%s,%s,%s::jsonb) RETURNING snapshot_id""",
                (observed, str(snapshot.get("terminal_server", ""))[:255], account_mode, symbol, digest, encoded),
            ).fetchone()
        return {"snapshot_id": str(row[0]), "snapshot_sha256": digest, "status": snapshot.get("status", "UNKNOWN")}

    def get_latest_mt5_risk_snapshot(self, symbol: str) -> dict[str, Any] | None:
        """Fetch the newest snapshot for a symbol without treating age as valid; caller gates freshness."""
        symbol = str(symbol).strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        with self._connect() as conn:
            row = conn.execute(
                """SELECT observed_at, account_mode, symbol, snapshot_sha256, snapshot
                   FROM mt5_risk_snapshots WHERE symbol=%s ORDER BY observed_at DESC, inserted_at DESC
                   LIMIT 1""", (symbol,)
            ).fetchone()
        if row is None:
            return None
        raw_snapshot = row[4]
        if isinstance(raw_snapshot, Mapping):
            snapshot = raw_snapshot
        elif isinstance(raw_snapshot, (str, bytes, bytearray)):
            try:
                snapshot = json.loads(raw_snapshot)
            except (TypeError, ValueError, UnicodeDecodeError) as exc:
                raise RuntimeError("persisted MT5 risk snapshot is not valid JSON") from exc
        else:
            raise RuntimeError("persisted MT5 risk snapshot is not a JSON object")
        if not isinstance(snapshot, Mapping):
            raise RuntimeError("persisted MT5 risk snapshot is not a JSON object")
        output = dict(snapshot)
        stored_digest = str(row[3])
        actual_digest = _sha256(_canonical(output))
        if (len(stored_digest) != 71 or not stored_digest.startswith("sha256:")
                or stored_digest != actual_digest):
            raise RuntimeError("persisted MT5 risk snapshot hash mismatch")
        payload_observed = _timestamp(output.get("observed_at"), "snapshot.observed_at")
        column_observed = _timestamp(row[0], "database.observed_at")
        if payload_observed != column_observed:
            raise RuntimeError("persisted MT5 risk snapshot timestamp column mismatch")
        payload_mode = str(output.get("account_mode", "unknown")).lower()
        if payload_mode not in {"demo", "contest", "real", "unknown"}:
            payload_mode = "unknown"
        if payload_mode != str(row[1]).lower():
            raise RuntimeError("persisted MT5 risk snapshot account-mode column mismatch")
        payload_symbol = str(output.get("symbol", "")).strip().upper() or "UNKNOWN"
        if payload_symbol != str(row[2]).strip().upper():
            raise RuntimeError("persisted MT5 risk snapshot symbol column mismatch")
        output["snapshot_sha256"] = stored_digest
        return output

    def persist_pipeline_result(
        self, result: Mapping[str, Any], *, signal_input_sha256: str,
    ) -> dict[str, Any]:
        """Atomically persist a verified pipeline audit chain and its immutable decision result."""
        if not isinstance(result, Mapping):
            raise ValueError("pipeline result must be an object")
        signal_id = str(result.get("signal_id", "")).strip()
        status = str(result.get("status", "")).upper()
        allowed_statuses = {"GATHER_MORE", "ESCALATE", "HOLD", "REJECT", "DENY",
                             "REQUIRE_HUMAN", "AUTHORIZED_ONCE"}
        if not signal_id or status not in allowed_statuses:
            raise ValueError("pipeline result has missing signal_id or unsupported status")
        if (not isinstance(signal_input_sha256, str) or len(signal_input_sha256) != 71
                or not signal_input_sha256.startswith("sha256:")
                or any(ch not in "0123456789abcdef" for ch in signal_input_sha256[7:])):
            raise ValueError("signal_input_sha256 must use sha256:<64 lowercase hex characters>")
        events = result.get("audit_events")
        if not isinstance(events, list) or not events:
            raise ValueError("pipeline result must contain a non-empty audit_events list")
        chain = verify_audit_chain(events)
        if chain.get("valid") is not True:
            raise ValueError("pipeline audit chain is invalid; result was not persisted")
        if result.get("audit_chain_valid") is not True:
            raise ValueError("pipeline result did not mark its audit chain valid")

        result_json = _canonical(dict(result))
        result_hash = _sha256(result_json)
        blockers = result.get("blockers", [])
        if not isinstance(blockers, list) or any(not isinstance(item, str) for item in blockers):
            raise ValueError("pipeline blockers must be an array of strings")
        last_hash = str(events[-1].get("event_hash", ""))
        event_count = len(events)

        with self._connect() as conn:
            inserted_events = 0
            duplicate_events = 0
            for event in events:
                if not isinstance(event, Mapping):
                    raise ValueError("audit event is not an object")
                event_type = str(event.get("event_type", "")).strip()
                event_hash = str(event.get("event_hash", ""))
                occurred_at = _timestamp(event.get("timestamp", ""), "audit_event.timestamp")
                previous_hash = event.get("previous_hash")
                payload = event.get("payload")
                if not event_type or not isinstance(payload, Mapping):
                    raise ValueError("audit event type or payload is invalid")
                encoded_payload = _canonical(dict(payload))
                returned = conn.execute(
                    """INSERT INTO audit_events
                       (event_type, schema_version, occurred_at, previous_hash, event_hash, payload)
                       VALUES (%s,1,%s,%s,%s,%s::jsonb)
                       ON CONFLICT (event_hash) DO NOTHING
                       RETURNING id""",
                    (event_type, occurred_at, previous_hash, event_hash, encoded_payload),
                ).fetchone()
                if returned:
                    inserted_events += 1
                    continue
                existing = conn.execute(
                    """SELECT event_type, schema_version, occurred_at, previous_hash, payload
                       FROM audit_events WHERE event_hash=%s""", (event_hash,)
                ).fetchone()
                if existing is None:
                    raise RuntimeError("audit event conflict row disappeared")
                existing_payload = existing[4] if isinstance(existing[4], Mapping) else json.loads(existing[4])
                if (existing[0] != event_type or int(existing[1]) != 1 or existing[2] != occurred_at
                        or existing[3] != previous_hash
                        or _canonical(dict(existing_payload)) != encoded_payload):
                    raise ValueError("audit event hash already exists with different persisted content")
                duplicate_events += 1

            inserted = conn.execute(
                """INSERT INTO decision_runs
                   (signal_id, signal_input_sha256, status, blockers, result_sha256,
                    last_audit_event_hash, audit_event_count, result)
                   VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb)
                   RETURNING decision_id, created_at""",
                (signal_id, signal_input_sha256, status, _canonical(blockers), result_hash,
                 last_hash, event_count, result_json),
            ).fetchone()
        return {
            "decision_id": str(inserted[0]), "created_at": inserted[1].isoformat(),
            "signal_id": signal_id, "status": status, "result_sha256": result_hash,
            "last_audit_event_hash": last_hash, "audit_event_count": event_count,
            "audit_events_inserted": inserted_events, "audit_events_already_present": duplicate_events,
            "trade_allowed": False,
        }

    def persist_event_once(self, event: Mapping[str, Any], *, consumer_name: str) -> bool:
        """Commit processed-event marker and event evidence atomically before Redis ACK."""
        if not isinstance(event, Mapping):
            raise ValueError("event must be an object")
        event_id = event.get("event_id")
        event_type = event.get("event_type")
        version = event.get("schema_version")
        if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 256:
            raise ValueError("event_id must be a non-empty string of at most 256 characters")
        if not isinstance(event_type, str) or not event_type.strip() or len(event_type) > 128:
            raise ValueError("event_type must be a non-empty string of at most 128 characters")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("schema_version must be a positive integer")
        if not isinstance(event.get("payload"), Mapping):
            raise ValueError("payload must be an object")
        raw_created_at = event.get("created_at")
        if not isinstance(raw_created_at, str) or not raw_created_at.strip():
            raise ValueError("created_at must be an ISO-8601 string")
        occurred_at = _timestamp(raw_created_at, "created_at")
        if (not isinstance(consumer_name, str) or not consumer_name.strip()
                or len(consumer_name) > 256):
            raise ValueError("consumer_name must be a non-empty string of at most 256 characters")
        try:
            _validate_event_json_value(event)
            encoded = json.dumps(
                dict(event), sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            )
            encoded_size = len(encoded.encode("utf-8"))
        except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError) as exc:
            raise ValueError("event must contain finite JSON-native values with string keys and depth <= 32") from exc
        if encoded_size > 256_000:
            raise ValueError("event payload exceeds 256 KB limit")
        digest = _sha256(encoded)
        with self._connect() as conn:
            inserted = conn.execute(
                """INSERT INTO processed_events(consumer_name, event_id, result_hash)
                   VALUES (%s,%s,%s) ON CONFLICT (consumer_name, event_id) DO NOTHING
                   RETURNING event_id""", (consumer_name, event_id, digest)
            ).fetchone()
            if not inserted:
                existing = conn.execute(
                    "SELECT result_hash FROM processed_events WHERE consumer_name=%s AND event_id=%s",
                    (consumer_name, event_id),
                ).fetchone()
                if not existing or existing[0] != digest:
                    raise ValueError("event_id replayed with a different payload")
                return False
            conn.execute(
                """INSERT INTO system_events
                   (event_id, consumer_name, event_type, schema_version, occurred_at, payload_sha256, event)
                   VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (event_id, consumer_name, event_type, version, occurred_at, digest, encoded),
            )
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply MT5 Critical Consensus database schema.")
    parser.add_argument("--migrate", action="store_true", help="apply base schema and SQL migrations")
    args = parser.parse_args()
    if not args.migrate:
        parser.error("specify --migrate")
    PostgresRepository().migrate()
    print("Database schema migrations applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
