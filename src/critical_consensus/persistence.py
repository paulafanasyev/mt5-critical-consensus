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
from typing import Any, Iterable, Mapping

from .market_data import MarketBar, validate_bar_series

ROOT = Path(__file__).resolve().parents[2]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False, default=str)


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

    def record_forecast(
        self, *, model_id: str, signal_id: str, issued_at: datetime | str,
        probability: float, input_sha256: str,
    ) -> bool:
        """Persist a probability at issue time; returns False for an identical replay."""
        model_id, signal_id = str(model_id).strip(), str(signal_id).strip()
        if not model_id or not signal_id:
            raise ValueError("model_id and signal_id are required")
        issued = _timestamp(issued_at, "issued_at")
        if isinstance(probability, bool) or not math.isfinite(float(probability)) or not 0 <= float(probability) <= 1:
            raise ValueError("probability must be finite and in [0,1]")
        if not isinstance(input_sha256, str) or len(input_sha256) != 71 or not input_sha256.startswith("sha256:"):
            raise ValueError("input_sha256 must use sha256:<64 hex characters>")
        if any(ch not in "0123456789abcdef" for ch in input_sha256[7:]):
            raise ValueError("input_sha256 must contain lowercase hex")
        with self._connect() as conn:
            inserted = conn.execute(
                """INSERT INTO forecast_predictions(model_id, signal_id, issued_at, probability, input_sha256)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT (model_id, signal_id) DO NOTHING
                   RETURNING signal_id""",
                (model_id, signal_id, issued, float(probability), input_sha256),
            ).fetchone()
            if inserted:
                return True
            old = conn.execute(
                """SELECT issued_at, probability, input_sha256 FROM forecast_predictions
                   WHERE model_id=%s AND signal_id=%s""", (model_id, signal_id)
            ).fetchone()
            if old is None:
                raise RuntimeError("forecast conflict row disappeared")
            if old[0] != issued or not math.isclose(float(old[1]), float(probability), rel_tol=0.0, abs_tol=0.0) or old[2] != input_sha256:
                raise ValueError("conflicting replay for immutable issued forecast")
            return False

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

    def persist_event_once(self, event: Mapping[str, Any], *, consumer_name: str) -> bool:
        """Commit processed-event marker and event evidence atomically before Redis ACK."""
        if not isinstance(event, Mapping):
            raise ValueError("event must be an object")
        event_id = str(event.get("event_id", "")).strip()
        event_type = str(event.get("event_type", "")).strip()
        version = event.get("schema_version")
        if not event_id or not event_type:
            raise ValueError("event_id and event_type are required")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("schema_version must be a positive integer")
        occurred_at = _timestamp(event.get("created_at", ""), "created_at")
        if not str(consumer_name).strip():
            raise ValueError("consumer_name is required")
        encoded = _canonical(dict(event))
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
