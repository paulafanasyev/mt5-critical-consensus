"""Persistence: sqlite for numbers, JSONL for the event trail.

A hundred accounts produce a lot of small facts; the fleet keeps them in one sqlite file so the
conductor can answer "what is the book doing" without waking every worker, and mirrors every
decision to a JSONL trail that survives crashes and is easy to grep.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    ts TEXT NOT NULL, account TEXT, kind TEXT NOT NULL, payload TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT NOT NULL, account TEXT NOT NULL, login INTEGER, balance REAL, equity REAL,
    positions INTEGER, floating REAL
);
CREATE TABLE IF NOT EXISTS trades (
    ts TEXT NOT NULL, account TEXT, login INTEGER, symbol TEXT, side TEXT, volume REAL,
    price REAL, sl REAL, tp REAL, retcode INTEGER, comment TEXT
);
CREATE TABLE IF NOT EXISTS daystate (
    account TEXT PRIMARY KEY, day TEXT, start_balance REAL, realized REAL, stopped INTEGER
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    account TEXT NOT NULL, ticket INTEGER NOT NULL, symbol TEXT, side TEXT, volume REAL,
    price_open REAL, profit REAL, sl REAL, tp REAL, updated TEXT,
    PRIMARY KEY (account, ticket)
);
CREATE TABLE IF NOT EXISTS peaks (
    account TEXT NOT NULL, ticket INTEGER NOT NULL, peak REAL, PRIMARY KEY (account, ticket)
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_equity_account ON equity(account, ts);
"""


class Store:
    def __init__(self, path: pathlib.Path | str) -> None:
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.trail = self.path.with_suffix(".jsonl")
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # --- writes -------------------------------------------------------------------------
    def event(self, account: str, kind: str, **payload) -> None:
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        record = {"ts": stamp, "account": account, "kind": kind, **payload}
        with self._connect() as conn:
            conn.execute("INSERT INTO events (ts, account, kind, payload) VALUES (?,?,?,?)",
                         (stamp, account, kind, json.dumps(payload, ensure_ascii=False)))
        with self.trail.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def equity(self, account: str, login: int, balance: float, equity: float,
               positions: int, floating: float) -> None:
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO equity (ts, account, login, balance, equity, positions, floating)"
                " VALUES (?,?,?,?,?,?,?)",
                (stamp, account, login, balance, equity, positions, floating))

    def trade(self, account: str, login: int, symbol: str, side: str, volume: float,
              price: float, sl: float, tp: float, retcode: int, comment: str) -> None:
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO trades (ts, account, login, symbol, side, volume, price, sl, tp,"
                " retcode, comment) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (stamp, account, login, symbol, side, volume, price, sl, tp, retcode, comment))

    def save_day(self, account: str, day: str, start_balance: float, realized: float,
                 stopped: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO daystate (account, day, start_balance, realized, stopped)"
                " VALUES (?,?,?,?,?) ON CONFLICT(account) DO UPDATE SET day=excluded.day,"
                " start_balance=excluded.start_balance, realized=excluded.realized,"
                " stopped=excluded.stopped",
                (account, day, start_balance, realized, int(stopped)))

    def save_positions(self, account: str, positions: list) -> None:
        """Replace the stored open-position snapshot for one account."""
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute("DELETE FROM positions WHERE account=?", (account,))
            conn.executemany(
                "INSERT INTO positions (account, ticket, symbol, side, volume, price_open,"
                " profit, sl, tp, updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(account, int(p.ticket), p.symbol, "buy" if p.type == 0 else "sell",
                  float(p.volume), float(p.price_open), float(p.profit), float(p.sl),
                  float(p.tp), stamp) for p in positions])

    def peak(self, account: str, ticket: int) -> float:
        with self._connect() as conn:
            row = conn.execute("SELECT peak FROM peaks WHERE account=? AND ticket=?",
                               (account, ticket)).fetchone()
        return float(row[0]) if row else 0.0

    def set_peak(self, account: str, ticket: int, peak: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO peaks (account, ticket, peak) VALUES (?,?,?)"
                " ON CONFLICT(account, ticket) DO UPDATE SET peak=excluded.peak",
                (account, ticket, peak))

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO meta (key, value) VALUES (?,?)"
                         " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def get_meta(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def open_positions(self) -> list[tuple[str, str, float]]:
        """Every known open position across accounts: (account, symbol, volume)."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT account, symbol, volume FROM positions").fetchall()

    # --- reads --------------------------------------------------------------------------
    def day(self, account: str) -> tuple[str, float, float, bool] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT day, start_balance, realized, stopped FROM daystate WHERE account=?",
                (account,)).fetchone()
        return (row[0], row[1], row[2], bool(row[3])) if row else None

    def portfolio(self) -> dict[str, float]:
        """Aggregate the whole book from each account's latest row.

        A single "latest equity" row would answer for one random account when several workers
        write in the same second, which would make the portfolio day stop fire on the equity of
        whatever account happened to be written last.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT SUM(balance), SUM(equity), SUM(positions), SUM(floating)"
                " FROM equity WHERE rowid IN (SELECT MAX(rowid) FROM equity GROUP BY account)"
            ).fetchone()
            accounts = conn.execute("SELECT COUNT(DISTINCT account) FROM equity").fetchone()[0]
        if not row or row[0] is None:
            return {"balance": 0.0, "equity": 0.0, "positions": 0.0, "floating": 0.0,
                    "accounts": float(accounts)}
        return {"balance": row[0] or 0.0, "equity": row[1] or 0.0,
                "positions": float(row[2] or 0), "floating": row[3] or 0.0,
                "accounts": float(accounts)}

    def latest_equity(self) -> list[tuple]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT account, login, balance, equity, positions, floating FROM equity"
                " WHERE rowid IN (SELECT MAX(rowid) FROM equity GROUP BY account)"
                " ORDER BY account").fetchall()

    def recent_events(self, limit: int = 20) -> list[tuple]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT ts, account, kind, payload FROM events ORDER BY rowid DESC LIMIT ?",
                (limit,)).fetchall()
