"""Append-only paper-betting ledger and demo wallet backed by SQLite.

Money is stored as integer units (the demo currency has no minor units). Every
stake and payout is a ledger entry; bets are never edited in place except for
the one allowed PENDING -> settled transition, which is idempotent.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator, Mapping

SELECTIONS = {"HOME": "H", "DRAW": "D", "AWAY": "A"}
RESULTS = {"H", "D", "A"}
STATUSES = {"PENDING", "WON", "LOST", "VOID"}


class LedgerError(RuntimeError):
    """Base error for the paper ledger."""


class IdempotencyConflict(LedgerError):
    """The same idempotency key was reused with different bet parameters."""


class InsufficientFunds(LedgerError):
    """The demo wallet cannot cover the requested stake."""


class PaperWallet:
    def __init__(self, db_path: str | Path = "sandbox_data/betting.db", *,
                 initial_balance_units: int = 1_000_000, currency: str = "VND") -> None:
        if isinstance(initial_balance_units, bool) or not isinstance(initial_balance_units, int) \
                or initial_balance_units < 0:
            raise ValueError("initial_balance_units must be a non-negative integer")
        if not currency or len(currency) > 8:
            raise ValueError("currency must be a short non-empty string")
        self._lock = threading.RLock()
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema(initial_balance_units, currency)

    # -- lifecycle ---------------------------------------------------------
    def _create_schema(self, initial_balance: int, currency: str) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS wallet (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    balance_units INTEGER NOT NULL CHECK (balance_units >= 0),
                    currency TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bets (
                    bet_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    match_id TEXT NOT NULL,
                    match_date TEXT NOT NULL,
                    division TEXT NOT NULL,
                    home TEXT NOT NULL,
                    away TEXT NOT NULL,
                    selection TEXT NOT NULL CHECK (selection IN ('HOME','DRAW','AWAY')),
                    odds TEXT NOT NULL,
                    stake_units INTEGER NOT NULL CHECK (stake_units > 0),
                    model_probability REAL NOT NULL,
                    market_probability REAL NOT NULL,
                    edge REAL NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('PENDING','WON','LOST','VOID')),
                    payout_units INTEGER NOT NULL DEFAULT 0,
                    result TEXT,
                    placed_at TEXT NOT NULL,
                    settled_at TEXT
                );
                CREATE TABLE IF NOT EXISTS ledger (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    amount_units INTEGER NOT NULL,
                    balance_after_units INTEGER NOT NULL,
                    bet_id TEXT,
                    note TEXT
                );
                """
            )
            row = self._conn.execute("SELECT id FROM wallet WHERE id = 1").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO wallet (id, balance_units, currency, updated_at) VALUES (1, ?, ?, ?)",
                    (initial_balance, currency, _now()),
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # -- wallet ------------------------------------------------------------
    def wallet(self) -> dict[str, object]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM wallet WHERE id = 1").fetchone()
        if row is None:
            raise LedgerError("wallet row is missing")
        return {"balance_units": int(row["balance_units"]),
                "currency": str(row["currency"]),
                "updated_at": str(row["updated_at"])}

    # -- betting -----------------------------------------------------------
    def place_bet(self, *, match_id: str, match_date: str, division: str, home: str,
                  away: str, selection: str, odds: float, stake_units: int,
                  model_probability: float, market_probability: float,
                  idempotency_key: str) -> dict[str, object]:
        selection = str(selection).upper()
        if selection not in SELECTIONS:
            raise ValueError("selection must be HOME, DRAW or AWAY")
        for name, value in (("match_id", match_id), ("match_date", match_date),
                            ("division", division), ("home", home), ("away", away),
                            ("idempotency_key", idempotency_key)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if isinstance(stake_units, bool) or not isinstance(stake_units, int) or stake_units <= 0:
            raise ValueError("stake_units must be a positive integer")
        odds_decimal = _decimal_odds(odds)
        model_probability = _probability(model_probability, "model_probability")
        market_probability = _probability(market_probability, "market_probability")
        edge = model_probability * float(odds_decimal) - 1.0

        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM bets WHERE idempotency_key = ?",
                                    (idempotency_key,)).fetchone()
            if existing is not None:
                if _same_request(existing, match_id, selection, odds_decimal, stake_units):
                    return _bet_row(existing)
                raise IdempotencyConflict(
                    "idempotency_key was already used with different bet parameters")

            wallet = conn.execute("SELECT balance_units FROM wallet WHERE id = 1").fetchone()
            if wallet is None:
                raise LedgerError("wallet row is missing")
            balance = int(wallet["balance_units"])
            if stake_units > balance:
                raise InsufficientFunds(f"stake {stake_units} exceeds demo balance {balance}")

            bet_id = uuid.uuid4().hex
            timestamp = _now()
            new_balance = balance - stake_units
            conn.execute("UPDATE wallet SET balance_units = ?, updated_at = ? WHERE id = 1",
                         (new_balance, timestamp))
            conn.execute(
                """INSERT INTO bets (bet_id, idempotency_key, match_id, match_date, division,
                                     home, away, selection, odds, stake_units, model_probability,
                                     market_probability, edge, status, payout_units, placed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?)""",
                (bet_id, idempotency_key, match_id, match_date, division, home, away,
                 selection, str(odds_decimal), stake_units, model_probability,
                 market_probability, edge, timestamp),
            )
            conn.execute(
                """INSERT INTO ledger (created_at, kind, amount_units, balance_after_units, bet_id, note)
                   VALUES (?, 'STAKE', ?, ?, ?, ?)""",
                (timestamp, -stake_units, new_balance, bet_id, f"{match_id}:{selection}"),
            )
            row = conn.execute("SELECT * FROM bets WHERE bet_id = ?", (bet_id,)).fetchone()
        return _bet_row(row)

    def settle_bet(self, bet_id: str, result: str) -> dict[str, object]:
        result = str(result).upper()
        if result not in RESULTS:
            raise ValueError("result must be H, D or A")
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM bets WHERE bet_id = ?", (bet_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown bet {bet_id}")
            if row["status"] != "PENDING":
                return _bet_row(row)
            won = SELECTIONS[str(row["selection"])] == result
            payout = int((Decimal(str(row["odds"])) * int(row["stake_units"])).quantize(Decimal("1"))) if won else 0
            timestamp = _now()
            status = "WON" if won else "LOST"
            conn.execute(
                "UPDATE bets SET status = ?, payout_units = ?, result = ?, settled_at = ? WHERE bet_id = ?",
                (status, payout, result, timestamp, bet_id),
            )
            if payout:
                wallet = conn.execute("SELECT balance_units FROM wallet WHERE id = 1").fetchone()
                new_balance = int(wallet["balance_units"]) + payout
                conn.execute("UPDATE wallet SET balance_units = ?, updated_at = ? WHERE id = 1",
                             (new_balance, timestamp))
                conn.execute(
                    """INSERT INTO ledger (created_at, kind, amount_units, balance_after_units, bet_id, note)
                       VALUES (?, 'PAYOUT', ?, ?, ?, ?)""",
                    (timestamp, payout, new_balance, bet_id, result),
                )
                row = conn.execute("SELECT * FROM bets WHERE bet_id = ?", (bet_id,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM bets WHERE bet_id = ?", (bet_id,)).fetchone()
        return _bet_row(row)

    def settle_match(self, match_id: str, result: str) -> list[dict[str, object]]:
        result = str(result).upper()
        if result not in RESULTS:
            raise ValueError("result must be H, D or A")
        with self._lock:
            pending = self._conn.execute(
                "SELECT bet_id FROM bets WHERE match_id = ? AND status = 'PENDING'",
                (match_id,),
            ).fetchall()
        return [self.settle_bet(str(row["bet_id"]), result) for row in pending]

    # -- reporting ---------------------------------------------------------
    def list_bets(self, *, limit: int = 100, status: str | None = None) -> list[dict[str, object]]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        query = "SELECT * FROM bets"
        params: list[object] = []
        if status is not None:
            status = str(status).upper()
            if status not in STATUSES:
                raise ValueError("unknown status")
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY placed_at DESC, bet_id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_bet_row(row) for row in rows]

    def balance_curve(self, *, limit: int = 5000) -> list[int]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            rows = self._conn.execute(
                "SELECT balance_after_units FROM ledger ORDER BY entry_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [int(row["balance_after_units"]) for row in rows]

    def stats(self) -> dict[str, object]:
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS n,
                          SUM(CASE WHEN status = 'PENDING' THEN 1 ELSE 0 END) AS pending,
                          SUM(CASE WHEN status = 'WON' THEN 1 ELSE 0 END) AS won,
                          SUM(CASE WHEN status = 'LOST' THEN 1 ELSE 0 END) AS lost,
                          SUM(CASE WHEN status != 'PENDING' THEN stake_units ELSE 0 END) AS staked,
                          SUM(CASE WHEN status != 'PENDING' THEN payout_units ELSE 0 END) AS returned
                   FROM bets"""
            ).fetchone()
            wallet = self._conn.execute("SELECT * FROM wallet WHERE id = 1").fetchone()
        settled = int(row["won"] or 0) + int(row["lost"] or 0)
        staked = int(row["staked"] or 0)
        returned = int(row["returned"] or 0)
        return {
            "bets": int(row["n"] or 0),
            "pending": int(row["pending"] or 0),
            "won": int(row["won"] or 0),
            "lost": int(row["lost"] or 0),
            "settled": settled,
            "staked_units": staked,
            "returned_units": returned,
            "profit_units": returned - staked,
            "roi": ((returned - staked) / staked) if staked else 0.0,
            "balance_units": int(wallet["balance_units"]),
            "currency": str(wallet["currency"]),
        }

    def reset(self, initial_balance_units: int) -> dict[str, object]:
        if isinstance(initial_balance_units, bool) or not isinstance(initial_balance_units, int) \
                or initial_balance_units < 0:
            raise ValueError("initial_balance_units must be a non-negative integer")
        with self._transaction() as conn:
            conn.execute("DELETE FROM bets")
            conn.execute("DELETE FROM ledger")
            timestamp = _now()
            conn.execute("UPDATE wallet SET balance_units = ?, updated_at = ? WHERE id = 1",
                         (initial_balance_units, timestamp))
            conn.execute(
                """INSERT INTO ledger (created_at, kind, amount_units, balance_after_units, note)
                   VALUES (?, 'RESET', ?, ?, 'demo wallet reset')""",
                (timestamp, initial_balance_units, initial_balance_units),
            )
        return self.stats()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _decimal_odds(value: float | Decimal) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("odds must be numeric, not boolean")
    try:
        odds = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("odds must be numeric") from exc
    if not odds.is_finite() or odds <= Decimal("1.0"):
        raise ValueError("odds must be finite and greater than 1")
    return odds


def _probability(value: float, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric, not boolean")
    probability = float(value)
    if not (0.0 <= probability <= 1.0) or probability != probability:
        raise ValueError(f"{name} must be in [0, 1]")
    return probability


def _same_request(row: Mapping[str, object], match_id: str, selection: str,
                  odds: Decimal, stake_units: int) -> bool:
    return (str(row["match_id"]) == match_id and str(row["selection"]) == selection
            and Decimal(str(row["odds"])) == odds and int(row["stake_units"]) == stake_units)


def _bet_row(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "bet_id": str(row["bet_id"]),
        "idempotency_key": str(row["idempotency_key"]),
        "match_id": str(row["match_id"]),
        "match_date": str(row["match_date"]),
        "division": str(row["division"]),
        "home": str(row["home"]),
        "away": str(row["away"]),
        "selection": str(row["selection"]),
        "odds": float(row["odds"]),
        "stake_units": int(row["stake_units"]),
        "model_probability": float(row["model_probability"]),
        "market_probability": float(row["market_probability"]),
        "edge": float(row["edge"]),
        "status": str(row["status"]),
        "payout_units": int(row["payout_units"]),
        "result": row["result"],
        "placed_at": str(row["placed_at"]),
        "settled_at": row["settled_at"],
    }
