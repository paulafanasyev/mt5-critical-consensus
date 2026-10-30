"""Atomic one-time nonce consumption for human authorization.

A valid-looking approval object is not proof that a permission was granted. The
authorization gate consumes an operator-supplied nonce exactly once through a store
that must be durable in production. A process-local store is supplied for tests and
single-process demos; it does not survive a restart.
"""
from __future__ import annotations

from datetime import datetime, timezone
import threading
from typing import Protocol


class NonceStore(Protocol):
    def consume(self, *, scope: str, nonce: str, expires_at: datetime,
                now: datetime | None = None) -> bool:
        """Atomically record a nonce; return False when already used or already expired."""


class InMemoryNonceStore:
    """Thread-safe, process-local nonce store for tests and single-process demos."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], datetime] = {}

    def consume(self, *, scope: str, nonce: str, expires_at: datetime,
                now: datetime | None = None) -> bool:
        scope = str(scope)
        nonce = str(nonce)
        if not scope or not nonce:
            raise ValueError("scope and nonce are required")
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        current = now if isinstance(now, datetime) else datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        expires_at = expires_at.astimezone(timezone.utc)
        current = current.astimezone(timezone.utc)
        with self._lock:
            key = (scope, nonce)
            if key in self._entries:
                return False
            if expires_at <= current:
                return False
            self._entries[key] = expires_at
            return True

    def purge_expired(self, *, now: datetime | None = None) -> int:
        """Explicitly drop expired tombstones; replay is denied before this can matter."""
        current = now if isinstance(now, datetime) else datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(timezone.utc)
        with self._lock:
            expired = [key for key, expiry in self._entries.items() if expiry <= current]
            for key in expired:
                del self._entries[key]
        return len(expired)


class PostgresNonceStore:
    """Durable store backed by the repository's atomic INSERT ... ON CONFLICT."""

    def __init__(self, repository) -> None:
        self._repository = repository

    def consume(self, *, scope: str, nonce: str, expires_at: datetime,
                now: datetime | None = None) -> bool:
        current = now if isinstance(now, datetime) else datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        if expires_at.astimezone(timezone.utc) <= current.astimezone(timezone.utc):
            return False
        return bool(self._repository.consume_authorization_nonce(
            scope=str(scope), nonce=str(nonce), expires_at=expires_at,
        ))
