"""Loopback HTTP bridge for the demo-only MetaTrader 5 EA.

The EA polls two endpoints on ``127.0.0.1``: the persisted kill-switch state and a
one-shot order command. This module implements exactly those two GETs, with a
header token, a single pending command, a short TTL and durable idempotency tombstones.
It never binds a non-loopback address, never accepts a write verb, and never logs the
token or the command body.

The bridge is part of the demo execution path in ``scripts/``; the order-free
``critical_consensus.mt5_agent`` package stays untouched.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

UTC = timezone.utc
BRIDGE_TOKEN_ENV = "MT5_BRIDGE_TOKEN"
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~-]{24,256}\Z")
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
MAX_TTL_SECONDS = 600
MIN_TTL_SECONDS = 5
KILL_SWITCH_PATH = "/v1/risk/kill-switch"
NEXT_COMMAND_PATH = "/v1/execution/next"
REQUIRED_KEYS = (
    "command_id", "client_order_id", "permission_id", "signal_id", "instrument",
    "direction", "size", "expected_price", "sl", "tp", "mode", "user_decision_id",
    "user_decision", "user_approved", "expires_at_epoch", "payload_hash", "created_at",
)
_STRING_KEYS = ("command_id", "client_order_id", "permission_id", "signal_id", "instrument",
                "direction", "mode", "user_decision_id", "user_decision", "payload_hash",
                "created_at")
_NUMBER_KEYS = ("size", "expected_price", "sl", "tp")


class BridgeError(RuntimeError):
    """A refused command or a bridge that cannot start; the reason is a machine code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def validate_token(token: str | None) -> str:
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise BridgeError("BRIDGE_TOKEN_INVALID",
                          "must be 24..256 characters of [A-Za-z0-9._~-]")
    return token


def _is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) \
        and value == value and value not in (float("inf"), float("-inf"))


def _flat_scalar(value: Any) -> bool:
    return isinstance(value, (str, bool, int, float)) and not isinstance(value, Mapping) \
        and not isinstance(value, (list, tuple))


def validate_command(command: Mapping[str, Any], *, now: float,
                     ttl_limit: int = MAX_TTL_SECONDS) -> dict[str, Any]:
    """Validate a command exactly as the EA will read it, before it enters the queue."""
    if not isinstance(command, Mapping):
        raise BridgeError("COMMAND_NOT_OBJECT")
    missing = [key for key in REQUIRED_KEYS if key not in command]
    if missing:
        raise BridgeError("COMMAND_MISSING_FIELDS", ",".join(missing))
    for key, value in command.items():
        if not isinstance(key, str) or not _flat_scalar(value):
            raise BridgeError("COMMAND_NOT_FLAT", str(key))
    for key in _STRING_KEYS:
        if not isinstance(command[key], str) or not command[key]:
            raise BridgeError("COMMAND_FIELD_INVALID", key)
        # The EA's strict flat parser rejects escapes and control characters, so a field
        # that cannot be serialized into its dialect must never reach the queue.
        if any(ord(char) < 0x20 or char in '\\"' for char in command[key]):
            raise BridgeError("COMMAND_FIELD_INVALID", key)
    for key in ("command_id", "client_order_id", "instrument"):
        if not IDENTIFIER_PATTERN.fullmatch(command[key]):
            raise BridgeError("COMMAND_IDENTIFIER_INVALID", key)
    if command["direction"] not in ("BUY", "SELL"):
        raise BridgeError("COMMAND_DIRECTION_INVALID")
    if command["mode"] != "demo":
        raise BridgeError("COMMAND_MODE_NOT_DEMO")
    if command["user_decision"] != "APPROVE" or command["user_approved"] is not True:
        raise BridgeError("COMMAND_NOT_APPROVED")
    for key in _NUMBER_KEYS:
        if not _is_number(command[key]) or command[key] <= 0:
            raise BridgeError("COMMAND_NUMBER_INVALID", key)
    expires = command["expires_at_epoch"]
    if isinstance(expires, bool) or not isinstance(expires, int):
        raise BridgeError("COMMAND_EXPIRY_INVALID")
    if not now < expires <= now + ttl_limit:
        raise BridgeError("COMMAND_EXPIRY_OUT_OF_RANGE")
    if not isinstance(command["created_at"], str) or len(command["created_at"]) > 64:
        raise BridgeError("COMMAND_FIELD_INVALID", "created_at")
    if not isinstance(command["payload_hash"], str) or len(command["payload_hash"]) > 128:
        raise BridgeError("COMMAND_FIELD_INVALID", "payload_hash")
    # Return a plain dict with exactly the fields the EA needs; extra caller fields would be
    # accepted by the EA's flat parser but are not part of the reviewed contract.
    return {key: command[key] for key in REQUIRED_KEYS}


def command_identifiers(command: Mapping[str, Any]) -> tuple[str, str]:
    return str(command["command_id"]), str(command["client_order_id"])


class TombstoneStore:
    """Durable record of every command/order id ever handed to the EA.

    One JSON object per line, flushed and fsynced before the command is served. A torn
    final line (a crash mid-append) is ignored; any other malformed line refuses the
    store, because an unreadable record means the duplicate check is meaningless.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._command_ids: set[str] = set()
        self._order_ids: set[str] = set()
        self._records: list[dict[str, Any]] = []
        if self._path.exists():
            self._load()

    def _load(self) -> None:
        raw = self._path.read_text(encoding="utf-8")
        lines = raw.splitlines()
        # A file whose last byte is not a newline may end in a torn write; it is the only
        # line that may be discarded. Everything else must parse.
        complete = raw.endswith("\n") or not lines
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                if not complete and index == len(lines) - 1:
                    continue
                raise BridgeError("TOMBSTONE_FILE_CORRUPT", f"line {index + 1}") from None
            if not isinstance(record, Mapping):
                raise BridgeError("TOMBSTONE_FILE_CORRUPT", f"line {index + 1}")
            command_id = record.get("command_id")
            order_id = record.get("client_order_id")
            if not isinstance(command_id, str) or not isinstance(order_id, str):
                raise BridgeError("TOMBSTONE_FILE_CORRUPT", f"line {index + 1}")
            self._command_ids.add(command_id)
            self._order_ids.add(order_id)
            self._records.append(dict(record))

    def records(self) -> list[dict[str, Any]]:
        """Copy of every persisted record, for startup reconciliation against the broker."""
        with self._lock:
            return [dict(record) for record in self._records]

    def contains(self, command_id: str, client_order_id: str) -> bool:
        with self._lock:
            return command_id in self._command_ids or client_order_id in self._order_ids

    def add(self, command_id: str, client_order_id: str, *, now: datetime) -> None:
        with self._lock:
            if command_id in self._command_ids or client_order_id in self._order_ids:
                raise BridgeError("DUPLICATE_COMMAND")
            self._path.parent.mkdir(parents=True, exist_ok=True)
            record_at = now.astimezone(UTC).isoformat()
            record = json.dumps({"command_id": command_id, "client_order_id": client_order_id,
                                 "at": record_at},
                                ensure_ascii=False, separators=(",", ":"))
            with self._path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(record + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._command_ids.add(command_id)
            self._order_ids.add(client_order_id)
            self._records.append({"command_id": command_id,
                                  "client_order_id": client_order_id,
                                  "at": record_at})


@dataclass
class QueuedCommand:
    command: dict[str, Any]
    queued_at: datetime
    served_at: datetime | None = None
    expired_at: datetime | None = None


@dataclass
class BridgeState:
    """Served/refused counters for the runner's summary; no tokens or payloads."""

    queued: int = 0
    served: int = 0
    expired: int = 0
    refused: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def refuse(self, code: str) -> None:
        self.refused += 1
        self.reasons[code] = self.reasons.get(code, 0) + 1


class DemoBridge:
    """A one-command queue plus the two loopback endpoints the EA polls."""

    def __init__(self, *, token: str, tombstones_path: str | Path,
                 kill_switch: Callable[[], Mapping[str, Any]],
                 host: str = "127.0.0.1", port: int = 8000,
                 command_ttl_seconds: int = 120,
                 clock: Callable[[], datetime] | None = None) -> None:
        if host not in ("127.0.0.1", "localhost"):
            raise BridgeError("BRIDGE_HOST_NOT_LOOPBACK")
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise BridgeError("BRIDGE_PORT_INVALID")
        if not isinstance(command_ttl_seconds, int) or isinstance(command_ttl_seconds, bool) \
                or not MIN_TTL_SECONDS <= command_ttl_seconds <= MAX_TTL_SECONDS:
            raise BridgeError("BRIDGE_TTL_INVALID")
        self._token = validate_token(token)
        self._kill_switch = kill_switch
        self._ttl = command_ttl_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._tombstones = TombstoneStore(tombstones_path)
        self._lock = threading.Lock()
        self._pending: QueuedCommand | None = None
        self._served: dict[str, datetime] = {}
        self._expired: dict[str, datetime] = {}
        self.state = BridgeState()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._host = host
        self._port = port

    # ------------------------------------------------------------------ lifecycle
    @property
    def port(self) -> int:
        if self._server is None:
            return self._port
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        host = "127.0.0.1" if self._host == "localhost" else self._host
        return f"http://{host}:{self.port}"

    def start(self) -> "DemoBridge":
        if self._server is not None:
            raise BridgeError("BRIDGE_ALREADY_STARTED")
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "CriticalConsensusDemoBridge/1"
            sys_version = ""

            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - stdlib API
                # Do not echo the request line: it is harmless here, but keeping the log to
                # the status only removes any chance of a future query string leaking.
                if bridge._debug_log is not None:
                    bridge._debug_log(f"{self.command} {self.path.split('?')[0]} "
                                      f"{getattr(self, '_status_code', '')}".strip())

            def do_GET(self) -> None:  # noqa: N802 - stdlib API
                self._dispatch()

            def do_POST(self) -> None:  # noqa: N802 - stdlib API
                self._reject_method()

            do_PUT = do_POST
            do_PATCH = do_POST
            do_DELETE = do_POST

            def _reject_method(self) -> None:
                self._status_code = 405
                self._send_json(405, {"error": "method_not_allowed"})

            def _dispatch(self) -> None:
                if not self._host_allowed():
                    self._status_code = 403
                    self._send_json(403, {"error": "host_not_allowed"})
                    return
                if not self._authorized():
                    self._status_code = 401
                    self._send_json(401, {"error": "unauthorized"})
                    return
                path = urlsplit(self.path).path
                if path == KILL_SWITCH_PATH:
                    payload = bridge._kill_switch_payload()
                    if payload is None:
                        self._status_code = 503
                        self._send_json(503, {"error": "kill_switch_unavailable"})
                        return
                    self._status_code = 200
                    self._send_json(200, payload)
                elif path == NEXT_COMMAND_PATH:
                    try:
                        command = bridge._next_command()
                    except BridgeError:
                        # A tombstone that cannot be persisted must not be handed out: the
                        # EA would send without a durable duplicate record. Fail closed.
                        bridge.state.refuse("TOMBSTONE_WRITE_FAILED")
                        self._status_code = 503
                        self._send_json(503, {"error": "tombstone_unavailable"})
                        return
                    if command is None:
                        self._status_code = 204
                        self.send_response(204)
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        return
                    self._status_code = 200
                    self._send_json(200, command)
                else:
                    self._status_code = 404
                    self._send_json(404, {"error": "not_found"})

            def _host_allowed(self) -> bool:
                host = (self.headers.get("Host") or "").strip().lower()
                if not host:
                    return False
                # Refuse DNS-rebinding style requests: the only hosts this bridge answers
                # for are the loopback names it is bound to.
                return host in {f"127.0.0.1:{bridge.port}", f"localhost:{bridge.port}"}

            def _authorized(self) -> bool:
                if self.headers.get("Origin"):
                    return False
                supplied = self.headers.get("X-Bridge-Key") or ""
                return hmac.compare_digest(supplied, bridge._token)

            def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                                  separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer((self._host, self._port), Handler)
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever,
                                        name="demo-bridge", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    def __enter__(self) -> "DemoBridge":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    _debug_log: Callable[[str], None] | None = None

    # ------------------------------------------------------------------ queue
    def enqueue(self, command: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and queue one command. Only one may be pending at a time."""
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise BridgeError("BRIDGE_CLOCK_NOT_AWARE")
        now = now.astimezone(UTC)
        try:
            clean = validate_command(command, now=now.timestamp(), ttl_limit=self._ttl)
        except BridgeError as exc:
            self.state.refuse(exc.code)
            raise
        command_id, order_id = command_identifiers(clean)
        if self._tombstones.contains(command_id, order_id):
            self.state.refuse("DUPLICATE_COMMAND")
            raise BridgeError("DUPLICATE_COMMAND")
        with self._lock:
            self._drop_expired(now)
            if self._pending is not None:
                self.state.refuse("QUEUE_BUSY")
                raise BridgeError("QUEUE_BUSY")
            # The tombstone is written when the command is served, not queued: a queued
            # command that expires untouched was never handed to the EA and may be re-created
            # with the same deterministic id on a later cycle.
            self._pending = QueuedCommand(command=clean, queued_at=now)
            self.state.queued += 1
        return dict(clean)

    def served_at(self, command_id: str) -> datetime | None:
        with self._lock:
            return self._served.get(command_id)

    def tombstone_records(self) -> list[dict[str, Any]]:
        """Every id this bridge has handed to an EA, for startup reconciliation."""
        return self._tombstones.records()

    def expired_at(self, command_id: str) -> datetime | None:
        with self._lock:
            return self._expired.get(command_id)

    def _drop_expired(self, now: datetime) -> None:
        pending = self._pending
        if pending is None:
            return
        if now.timestamp() >= pending.command["expires_at_epoch"]:
            self._pending = None
            self._expired[command_identifiers(pending.command)[0]] = now
            self.state.expired += 1

    def _next_command(self) -> dict[str, Any] | None:
        moment = self._clock()
        if moment.tzinfo is None or moment.utcoffset() is None:
            return None
        now = moment.astimezone(UTC)
        with self._lock:
            self._drop_expired(now)
            pending = self._pending
            if pending is None:
                return None
            command_id, order_id = command_identifiers(pending.command)
            # The tombstone is written here, before the body leaves the process: from this
            # point the EA may send, so a restart must not serve the same id again even if
            # the EA never logged a result.
            self._tombstones.add(command_id, order_id, now=now)
            self._pending = None
            pending.served_at = now
            self._served[command_id] = now
            self.state.served += 1
            return dict(pending.command)

    def _kill_switch_payload(self) -> dict[str, Any] | None:
        try:
            payload = self._kill_switch()
        except Exception:
            return None
        if not isinstance(payload, Mapping) or not isinstance(
                payload.get("kill_switch_active"), bool):
            return None
        clean: dict[str, Any] = {}
        for key, value in payload.items():
            if not isinstance(key, str) or not _flat_scalar(value):
                return None
            if isinstance(value, str):
                # The EA's strict parser rejects escapes and control characters; a reason
                # carrying one would block every poll instead of just displaying badly.
                if any(ord(char) < 0x20 or char in '\\"' for char in value):
                    value = re.sub(r'[\x00-\x1f\\"]', " ", value)
            clean[key] = value
        if "kill_switch_active" not in clean:
            return None
        return clean


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
