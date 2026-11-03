"""Persistence: a small book table for O(accounts) reads, slots for atomic limits, and an audit trail.

The equity history grows without bound, so the fleet never aggregates it for decisions: a `book`
row per account is upserted each cycle and the portfolio is a SUM over that table. Risk slots give
the portfolio limits a real reservation, so two workers cannot both pass the same check and then
open twice the cap.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    ts TEXT NOT NULL, account TEXT, kind TEXT NOT NULL, payload TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT NOT NULL, account TEXT NOT NULL, login INTEGER, balance REAL, equity REAL,
    positions INTEGER, floating REAL, currency TEXT, rate_to_usd REAL
);
CREATE TABLE IF NOT EXISTS book (
    account TEXT PRIMARY KEY, ts TEXT, login INTEGER, balance REAL, equity REAL, equity_usd REAL,
    positions INTEGER, floating REAL, currency TEXT, rate_to_usd REAL,
    server_day TEXT, updated_epoch REAL
);
CREATE TABLE IF NOT EXISTS trades (
    ts TEXT NOT NULL, account TEXT, login INTEGER, symbol TEXT, side TEXT, volume REAL,
    requested_price REAL, requested_volume REAL, retcode INTEGER, comment TEXT, token TEXT
);
CREATE TABLE IF NOT EXISTS daystate (
    account TEXT PRIMARY KEY, day TEXT, start_balance REAL, realized REAL, stopped INTEGER
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    account TEXT NOT NULL, ticket INTEGER NOT NULL, symbol TEXT, side TEXT, volume REAL,
    price_open REAL, profit REAL, sl REAL, tp REAL, magic INTEGER, updated_epoch REAL,
    PRIMARY KEY (account, ticket)
);
CREATE TABLE IF NOT EXISTS risk_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, symbol TEXT NOT NULL,
    volume REAL NOT NULL, symbol_class TEXT, created_epoch REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS peaks (
    account TEXT NOT NULL, ticket INTEGER NOT NULL, peak REAL, PRIMARY KEY (account, ticket)
);
CREATE TABLE IF NOT EXISTS health (
    account TEXT PRIMARY KEY, fail_count INTEGER DEFAULT 0, next_attempt_epoch REAL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);
"""

RETENTION_DAYS = 30
POSITIONS_TTL_SECONDS = 900


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
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    # --- writes -------------------------------------------------------------------------
    def event(self, account: str, kind: str, **payload) -> None:
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        record = {"ts": stamp, "account": account, "kind": kind, **payload}
        for attempt in range(3):
            try:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT INTO events (ts, account, kind, payload) VALUES (?,?,?,?)",
                        (stamp, account, kind,
                         json.dumps(payload, ensure_ascii=False, default=str)))
                break
            except sqlite3.OperationalError:
                if attempt == 2:
                    return
                time.sleep(0.2 * (attempt + 1))
        try:
            with self.trail.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def equity(self, account: str, login: int, balance: float, equity: float,
               positions: int, floating: float, currency: str = "USD",
               rate_to_usd: float = 1.0, server_day: str = "", ) -> None:
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        epoch = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO equity (ts, account, login, balance, equity, positions, floating,"
                " currency, rate_to_usd) VALUES (?,?,?,?,?,?,?,?,?)",
                (stamp, account, login, balance, equity, positions, floating, currency,
                 rate_to_usd or None))
            conn.execute(
                "INSERT INTO book (account, ts, login, balance, equity, equity_usd, positions,"
                " floating, currency, rate_to_usd, server_day, updated_epoch)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(account) DO UPDATE SET ts=excluded.ts, login=excluded.login,"
                " balance=excluded.balance, equity=excluded.equity, equity_usd=excluded.equity_usd,"
                " positions=excluded.positions, floating=excluded.floating,"
                " currency=excluded.currency, rate_to_usd=excluded.rate_to_usd,"
                " server_day=excluded.server_day, updated_epoch=excluded.updated_epoch",
                (account, stamp, login, balance, equity,
                 equity * rate_to_usd if rate_to_usd > 0 else None, positions,
                 floating, currency, rate_to_usd, server_day, epoch))

    def trade(self, account: str, login: int, symbol: str, side: str, volume: float,
              requested_price: float, requested_volume: float, retcode: int, comment: str,
              token: str = "") -> None:
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO trades (ts, account, login, symbol, side, volume, requested_price,"
                " requested_volume, retcode, comment, token) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (stamp, account, login, symbol, side, volume, requested_price,
                 requested_volume, retcode, comment, token))

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
        """Replace this account's mirror of open positions (magic and freshness included)."""
        epoch = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM positions WHERE account=?", (account,))
            conn.executemany(
                "INSERT INTO positions (account, ticket, symbol, side, volume, price_open,"
                " profit, sl, tp, magic, updated_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(account, int(p.ticket), p.symbol, "buy" if p.type == 0 else "sell",
                  float(p.volume), float(p.price_open), float(p.profit), float(p.sl),
                  float(p.tp), int(getattr(p, "magic", 0)), epoch) for p in positions])

    def close_position_row(self, account: str, ticket: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM positions WHERE account=? AND ticket=?",
                         (account, ticket))
            conn.execute("DELETE FROM peaks WHERE account=? AND ticket=?", (account, ticket))

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

    def health(self, account: str) -> tuple[int, float, str]:
        with self._connect() as conn:
            row = conn.execute("SELECT fail_count, next_attempt_epoch, last_error FROM health"
                               " WHERE account=?", (account,)).fetchone()
        return (row[0], row[1], row[2]) if row else (0, 0.0, "")

    def record_failure(self, account: str, error: str, backoff_seconds: float) -> int:
        failures, _, _ = self.health(account)
        failures += 1
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO health (account, fail_count, next_attempt_epoch, last_error)"
                " VALUES (?,?,?,?) ON CONFLICT(account) DO UPDATE SET"
                " fail_count=excluded.fail_count, next_attempt_epoch=excluded.next_attempt_epoch,"
                " last_error=excluded.last_error",
                (account, failures, time.time() + backoff_seconds, error[:400]))
        return failures

    def clear_failure(self, account: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE health SET fail_count=0, next_attempt_epoch=0, last_error=''"
                         " WHERE account=?", (account,))

    # --- portfolio limits: atomic reservation ------------------------------------------
    def reserve(self, account: str, symbol: str, volume: float, limits,
                equity_usd: float) -> tuple[bool, str, int]:
        """Atomically reserve volume against the portfolio caps; returns (ok, reason, slot_id)."""
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM risk_slots WHERE created_epoch < ?", (now - 600,))
                rows = conn.execute("SELECT symbol, volume FROM risk_slots").fetchall()
                gold = sum(v for s, v in rows if _is_gold(s))
                usd = sum(v for s, v in rows if _is_usd_pair(s))
                total_volume = sum(v for _, v in rows)
                if gold + (volume if _is_gold(symbol) else 0) > limits.max_gold_lots:
                    conn.execute("ROLLBACK")
                    return False, "лимит по золоту (с резервами)", 0
                if usd + (volume if _is_usd_pair(symbol) else 0) > limits.max_usd_lots:
                    conn.execute("ROLLBACK")
                    return False, "лимит по долларовым парам (с резервами)", 0
                if total_volume + volume > limits.max_usd_lots * 4:
                    conn.execute("ROLLBACK")
                    return False, "лимит суммарного объёма (с резервами)", 0
                cursor = conn.execute(
                    "INSERT INTO risk_slots (account, symbol, volume, symbol_class,"
                    " created_epoch) VALUES (?,?,?,?,?)",
                    (account, symbol, volume,
                     "gold" if _is_gold(symbol) else "usd" if _is_usd_pair(symbol) else "other",
                     now))
                conn.execute("COMMIT")
                return True, "", int(cursor.lastrowid or 0)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def release(self, slot_id: int) -> None:
        if slot_id <= 0:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM risk_slots WHERE id=?", (slot_id,))

    def reserved_volume(self, symbol_class: str = "") -> float:
        with self._connect() as conn:
            if symbol_class:
                row = conn.execute("SELECT SUM(volume) FROM risk_slots WHERE symbol_class=?",
                                   (symbol_class,)).fetchone()
            else:
                row = conn.execute("SELECT SUM(volume) FROM risk_slots").fetchone()
        return float(row[0] or 0.0)

    # --- reads --------------------------------------------------------------------------
    def day(self, account: str) -> tuple[str, float, float, bool] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT day, start_balance, realized, stopped FROM daystate WHERE account=?",
                (account,)).fetchone()
        return (row[0], row[1], row[2], bool(row[3])) if row else None

    def portfolio(self, active: list[str] | None = None,
                  fresh_only: bool = True) -> dict[str, float]:
        """The book in USD; the stop uses fresh_only=False so a stale account is not forgotten."""
        now = time.time() - 3600 if fresh_only else 0.0
        with self._connect() as conn:
            if active:
                marks = ",".join("?" for _ in active)
                row = conn.execute(
                    "SELECT COUNT(*), SUM(balance * rate_to_usd), SUM(equity_usd),"
                    " SUM(positions), SUM(floating * rate_to_usd) FROM book"
                    f" WHERE updated_epoch > ? AND account IN ({marks})",
                    (now, *active)).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*), SUM(balance * rate_to_usd), SUM(equity_usd),"
                    " SUM(positions), SUM(floating * rate_to_usd),"
                    " SUM(CASE WHEN equity_usd IS NULL THEN 1 ELSE 0 END) FROM book"
                    " WHERE updated_epoch > ?", (now,)).fetchone()
        if not row or not row[0]:
            return {"balance": 0.0, "equity": 0.0, "positions": 0.0, "floating": 0.0,
                    "accounts": 0.0, "unpriced": 0.0}
        return {"balance": row[1] or 0.0, "equity": row[2] or 0.0,
                "positions": float(row[3] or 0), "floating": row[4] or 0.0,
                "accounts": float(row[0]),
                "unpriced": float(row[5] or 0) if len(row) > 5 else 0.0}

    def account_equity(self, account: str) -> float:
        with self._connect() as conn:
            row = conn.execute("SELECT equity_usd FROM book WHERE account=?",
                               (account,)).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    def latest_equity(self) -> list[tuple]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT account, login, balance, equity, positions, floating, currency"
                " FROM book ORDER BY account").fetchall()

    def open_positions(self) -> list[tuple[str, str, float, int, float]]:
        """Fresh mirror rows only: (account, symbol, volume, magic, age_seconds)."""
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT account, symbol, volume, magic, updated_epoch FROM positions").fetchall()
        return [(a, s, v, m, now - (u or 0)) for a, s, v, m, u in rows
                if now - (u or 0) <= POSITIONS_TTL_SECONDS]

    def all_positions(self, max_age: float = POSITIONS_TTL_SECONDS) -> list[tuple]:
        """Mirror rows fresh enough to trust; a long-silent account's ghosts stop counting."""
        cutoff = time.time() - max_age
        with self._connect() as conn:
            return conn.execute(
                "SELECT account, ticket, symbol, side, volume, price_open, profit, sl, tp, magic"
                " FROM positions WHERE updated_epoch >= ?", (cutoff,)).fetchall()

    def recent_events(self, limit: int = 20) -> list[tuple]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT ts, account, kind, payload FROM events ORDER BY rowid DESC LIMIT ?",
                (limit,)).fetchall()

    def prune(self, active: list[str] | None = None,
              retention_days: int = RETENTION_DAYS) -> int:
        """Drop old history and orphaned mirrors; registered accounts keep their rows."""
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(days=retention_days)).isoformat(timespec="seconds")
        epoch_cutoff = time.time() - POSITIONS_TTL_SECONDS
        with self._connect() as conn:
            removed = conn.execute("DELETE FROM equity WHERE ts < ?", (cutoff,)).rowcount
            conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            if active:
                marks = ",".join("?" for _ in active)
                conn.execute(
                    f"DELETE FROM positions WHERE updated_epoch < ? AND account NOT IN ({marks})",
                    (epoch_cutoff, *active))
                conn.execute(f"DELETE FROM book WHERE account NOT IN ({marks})", (*active,))
                conn.execute(f"DELETE FROM peaks WHERE account NOT IN ({marks})", (*active,))
                conn.execute(f"DELETE FROM health WHERE account NOT IN ({marks})", (*active,))
            else:
                conn.execute("DELETE FROM positions WHERE updated_epoch < ?", (epoch_cutoff,))
            conn.execute("DELETE FROM peaks WHERE (account, ticket) NOT IN"
                         " (SELECT account, ticket FROM positions)")
            conn.execute("DELETE FROM risk_slots WHERE created_epoch < ?",
                         (time.time() - 600,))
            conn.execute("DELETE FROM meta WHERE key LIKE 'dayflows:%' AND key NOT LIKE ?",
                         (f"dayflows:%:{dt.datetime.now(dt.timezone.utc).date().isoformat()}",))
            conn.execute("DELETE FROM meta WHERE key LIKE 'netflow:%'")
        return removed


def _is_gold(symbol: str) -> bool:
    upper = symbol.upper()
    return "XAU" in upper or "GOLD" in upper


def _is_usd_pair(symbol: str) -> bool:
    upper = symbol.upper()
    return (upper.startswith("USD") or upper.endswith("USD")) and not _is_gold(upper)
