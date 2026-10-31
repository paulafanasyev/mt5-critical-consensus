"""Append-only SQLite journal for agent decisions and paper fills.

Every row is part of one hash chain, equity points included, so a report can be checked
against an anchor hash the operator kept from an earlier run. There is no second,
unverified table an edit could hide in.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..audit import make_audit_event, verify_audit_chain

EQUITY_EVENT_TYPE = "equity_point"
SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  previous_hash TEXT,
  event_hash TEXT NOT NULL UNIQUE,
  run_id TEXT,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_events_type_idx ON audit_events (event_type, seq);
CREATE INDEX IF NOT EXISTS audit_events_run_idx ON audit_events (run_id, seq);
"""


def _requires_json_number(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("equity and balance must be numbers")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError("equity and balance must be finite")
    return number


class DecisionJournal:
    """Journal of one decision log.

    ``run_id`` is written on every event so a report can be confined to one run without
    pretending the file only ever held that run.
    """

    def __init__(self, db_path: str | Path, *, run_id: str | None = None) -> None:
        self._path = Path(db_path)
        if self._path.parent and not self._path.parent.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._run_id = None if run_id is None else self._validate_run_id(run_id)
        self._connection = sqlite3.connect(str(self._path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock, self._connection:
            self._connection.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        columns = {row["name"] for row in
                   self._connection.execute("PRAGMA table_info(audit_events)")}
        if "run_id" not in columns:
            # Journals written before run ids existed stay readable; their events simply
            # carry a null run and are excluded from run-scoped reports.
            self._connection.execute("ALTER TABLE audit_events ADD COLUMN run_id TEXT")

    @staticmethod
    def _validate_run_id(run_id: str) -> str:
        text = str(run_id).strip()
        if not text or len(text) > 64 or any(char in text for char in ("\x00", "\n", "\r")):
            raise ValueError("run_id must be a short single-line identifier")
        return text

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def last_hash(self) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT event_hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
        return None if row is None else str(row["event_hash"])

    def record(self, event_type: str, payload: Mapping[str, Any], *,
               run_id: str | None = None) -> dict[str, Any]:
        if not str(event_type).strip():
            raise ValueError("event_type must be non-empty")
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping")
        event_run = self._run_id if run_id is None else self._validate_run_id(run_id)
        with self._lock, self._connection:
            previous = self.last_hash()
            event = make_audit_event(str(event_type), dict(payload), previous_hash=previous,
                                     run_id=event_run)
            self._connection.execute(
                "INSERT INTO audit_events (event_type, recorded_at, previous_hash, event_hash,"
                " run_id, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (event["event_type"], event["timestamp"], event["previous_hash"],
                 event["event_hash"], event.get("run_id"),
                 json.dumps(event["payload"], sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False, allow_nan=False)))
            return event

    def record_equity(self, *, equity: float, balance: float, open_positions: int,
                      at: datetime | None = None,
                      execution_mode: str | None = None) -> dict[str, Any]:
        """Chained equity point: the run's drawdown figures come from these events."""
        if open_positions < 0 or isinstance(open_positions, bool):
            raise ValueError("open_positions must be a non-negative integer")
        moment = at or datetime.now(timezone.utc)
        # ``astimezone`` silently assumes the machine's local zone for a naive value, so the
        # check has to happen before the conversion, not on its result.
        if not isinstance(moment, datetime) or moment.tzinfo is None \
                or moment.utcoffset() is None:
            raise ValueError("equity point timestamps must be timezone-aware")
        moment = moment.astimezone(timezone.utc)
        payload: dict[str, Any] = {
            "equity": _requires_json_number(equity), "balance": _requires_json_number(balance),
            "open_positions": int(open_positions), "at": moment.isoformat(),
            # Every chained row states this, so no reader has to infer it per event type.
            "broker_order_sent": False}
        if execution_mode is not None:
            payload["execution_mode"] = str(execution_mode)
        return self.record(EQUITY_EVENT_TYPE, payload)

    def events(self, *, limit: int | None = 1000, event_type: str | None = None,
               run_id: str | None = None) -> list[dict[str, Any]]:
        if limit is not None and int(limit) < 1:
            raise ValueError("limit must be a positive integer or None")
        query = ("SELECT event_type, recorded_at, previous_hash, event_hash, run_id, payload"
                 " FROM audit_events")
        clauses: list[str] = []
        params: list[Any] = []
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        # ``None`` means the whole chain: a verification that silently stopped at a row count
        # would call a truncated prefix valid.
        query += " ORDER BY seq DESC" + ("" if limit is None else " LIMIT ?")
        if limit is not None:
            params.append(int(limit))
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [self._row_event(row) for row in reversed(rows)]

    @staticmethod
    def _row_event(row: sqlite3.Row) -> dict[str, Any]:
        # Only fields that took part in the hash may appear here: verify_audit_chain re-hashes
        # everything except event_hash. New events sign their run_id, legacy (null) rows did
        # not, so the key is returned only when the column is populated.
        event = {"event_type": row["event_type"], "timestamp": row["recorded_at"],
                 "previous_hash": row["previous_hash"], "event_hash": row["event_hash"],
                 "payload": json.loads(row["payload"])}
        if row["run_id"] is not None:
            event["run_id"] = row["run_id"]
        return event

    def chain_status(self, *, run_id: str | None = None,
                     anchor: str | None = None) -> dict[str, Any]:
        """Verify the chain, optionally only the events of one run starting at ``anchor``.

        Without an anchor this proves the file is internally consistent. A tamperer who
        deletes a prefix or rebuilds the chain passes that check, so an operator who kept
        the ``anchor`` printed by an earlier run should pass it here.
        """
        rows = self.events(limit=None, run_id=run_id)
        if anchor is not None and not rows:
            # A kept anchor plus an empty file is the strongest prefix deletion there is: an
            # empty chain cannot match a hash the operator saw earlier.
            expected = anchor
            result = {"valid": False, "index": 0, "reason": "PREVIOUS_HASH_MISMATCH"}
        else:
            expected = anchor if anchor is not None else (
                rows[0]["previous_hash"] if rows else None)
            result = verify_audit_chain(rows, initial_previous_hash=expected)
        result["events"] = len(rows)
        result["run_id"] = run_id
        result["anchor"] = expected
        # Self-anchored on the first surviving row's previous_hash, a deleted prefix still
        # verifies. Only an anchor kept outside the file proves that nothing was removed.
        result["anchor_source"] = "provided" if anchor is not None else "first_event_previous_hash"
        result["first_hash"] = rows[0]["event_hash"] if rows else None
        result["last_hash"] = rows[-1]["event_hash"] if rows else None
        return result

    def verify_chain(self) -> dict[str, Any]:
        return self.chain_status()

    def equity_curve(self, *, run_id: str | None = None) -> list[float]:
        return [float(event["payload"]["equity"])
                for event in self.events(limit=None, event_type=EQUITY_EVENT_TYPE,
                                         run_id=run_id)]

    def report(self, *, initial_equity: float | None = None,
               run_id: str | None = None) -> dict[str, Any]:
        if self._run_id is not None and run_id is None:
            run_id = self._run_id
        events = self.events(limit=None, run_id=run_id)
        counts: dict[str, int] = {}
        rejected: dict[str, int] = {}
        cycles = signals = 0
        for event in events:
            counts[event["event_type"]] = counts.get(event["event_type"], 0) + 1
            if event["event_type"] != "agent_cycle":
                continue
            payload = event["payload"]
            cycles += 1
            if payload.get("proposal") is not None:
                signals += 1
            for blocker in payload.get("blockers", []):
                rejected[str(blocker)] = rejected.get(str(blocker), 0) + 1
        curve = [float(event["payload"]["equity"]) for event in events
                 if event["event_type"] == EQUITY_EVENT_TYPE]
        if initial_equity is None:
            # Guessing a balance (the CLI used to assume 10000) silently rescales drawdown for
            # a run that started elsewhere; the first recorded point is the honest fallback.
            initial_equity = curve[0] if curve else 0.0
        peak = initial_equity
        drawdown = 0.0
        for value in curve:
            peak = max(peak, value)
            drawdown = max(drawdown, peak - value)
        first_hash = events[0]["previous_hash"] if events else None
        return {"event_counts": counts, "cycles": cycles, "cycles_with_signal": signals,
                "blocker_histogram": dict(sorted(rejected.items())),
                "run_id": run_id, "equity_points": len(curve),
                "initial_equity": initial_equity,
                "final_equity": curve[-1] if curve else initial_equity,
                "max_drawdown_account_currency": drawdown,
                "anchor_hash": first_hash,
                "broker_order_sent": False,
                # Self-anchored on the first surviving row: the report proves the file is
                # internally consistent, not that nothing was deleted. ``anchor_source`` says
                # so explicitly; only --verify-anchor supplies an external anchor.
                "verified_chain": self.chain_status(run_id=run_id)}

    def reset(self) -> None:
        """Delete every journaled event. The chain restarts, so old anchors no longer apply."""
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM audit_events")
            self._connection.execute("DELETE FROM sqlite_sequence WHERE name = 'audit_events'")
