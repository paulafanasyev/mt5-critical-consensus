"""The demo bridge must be a one-shot, loopback-only, authenticated door.

These tests drive the bridge over real sockets, because the properties that matter —
Host and Origin refusal, header authentication, single-pending-command semantics and
durable tombstones across restarts — live in the HTTP layer, not in a pure function.
A command that expires in the queue was never handed to the EA, so it must stay
re-creatable; a command that was served must never be served twice, even after a
restart of the process.
"""
from __future__ import annotations

import functools
import http.client
import importlib.util
import json
import select
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

UTC = timezone.utc
TOKEN = "a" * 43


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


@functools.lru_cache(maxsize=1)
def _bridge():
    path = _root() / "scripts" / "mt5_demo_bridge.py"
    spec = importlib.util.spec_from_file_location("mt5_demo_bridge", path)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules[cls.__module__], so the
    # module must be registered before its body runs, not only after.
    sys.modules["mt5_demo_bridge"] = module
    spec.loader.exec_module(module)
    return module


class FakeClock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment = self.moment + timedelta(seconds=seconds)


def _command(now: datetime, *, command_id: str = "cmd-1", client_order_id: str = "ord-1",
             ttl: int = 60, **overrides):
    base = {
        "command_id": command_id,
        "client_order_id": client_order_id,
        "permission_id": "perm-1",
        "signal_id": "sig-1",
        "instrument": "EURUSD",
        "direction": "BUY",
        "size": 0.01,
        "expected_price": 1.085,
        "sl": 1.08,
        "tp": 1.095,
        "mode": "demo",
        "user_decision_id": "dec-1",
        "user_decision": "APPROVE",
        "user_approved": True,
        "expires_at_epoch": int(now.timestamp()) + ttl,
        "payload_hash": "f" * 64,
        "created_at": now.isoformat(),
    }
    base.update(overrides)
    return base


def _request(bridge, method: str, path: str, *, key: str | None = TOKEN,
             headers: dict | None = None, body: str | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=5)
    request_headers = {} if headers is None else dict(headers)
    if key is not None:
        request_headers["X-Bridge-Key"] = key
    if body is not None:
        request_headers.setdefault("Content-Type", "application/json")
        request_headers["Content-Length"] = str(len(body.encode("utf-8")))
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    payload = response.read()
    status = response.status
    response_headers = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    return status, payload, response_headers


def _post(bridge, path: str, payload, *, key: str | None = TOKEN,
          headers: dict | None = None, raw: str | None = None):
    """Send a POST the way an operator's script does: JSON body, bridge key in a header."""
    body = raw if raw is not None else json.dumps(payload)
    return _request(bridge, "POST", path, key=key, headers=headers, body=body)


def _arm(store, *, reason: str = "operator", active: bool = True):
    store.record(active=active, actor_id="test", reason=reason, now=datetime(
        2026, 10, 31, 12, 0, tzinfo=UTC))
    return store


def _json(body: bytes):
    return json.loads(body.decode("utf-8"))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def make_bridge(tmp_path, clock):
    created = []

    def factory(**kwargs):
        store = kwargs.pop("kill_switch_store", None) or _bridge().KillSwitchStore(
            tmp_path / "kill_switch.json")
        options = {
            "token": TOKEN,
            "tombstones_path": tmp_path / "tombstones.jsonl",
            "kill_switch_store": store,
            "host": "127.0.0.1",
            "port": 0,
            "command_ttl_seconds": 120,
            "admin_token": lambda: "",
            "clock": clock,
        }
        options.update(kwargs)
        instance = _bridge().DemoBridge(**options)
        created.append(instance)
        return instance

    yield factory
    for instance in created:
        instance.stop()


# ------------------------------------------------------------------ token and bind
def test_token_shape_is_enforced():
    module = _bridge()
    for bad in (None, "", "short", "х" * 30, "a" * 23, "a" * 257, "bad token with spaces"):
        with pytest.raises(module.BridgeError) as error:
            module.validate_token(bad)
        assert error.value.code == "BRIDGE_TOKEN_INVALID"
    assert module.validate_token(TOKEN) == TOKEN


def test_bridge_refuses_a_non_loopback_bind_and_bad_port(tmp_path, clock):
    module = _bridge()
    store = module.KillSwitchStore(tmp_path / "kill_switch.json")
    for host in ("0.0.0.0", "::", "10.0.0.5", "example.com"):
        with pytest.raises(module.BridgeError) as error:
            module.DemoBridge(token=TOKEN, tombstones_path=tmp_path / "t.jsonl",
                              kill_switch_store=store, host=host, port=0, clock=clock)
        assert error.value.code == "BRIDGE_HOST_NOT_LOOPBACK"
    for port in (-1, 70000, True, "8000"):
        with pytest.raises(module.BridgeError) as error:
            module.DemoBridge(token=TOKEN, tombstones_path=tmp_path / "t.jsonl",
                              kill_switch_store=store, host="127.0.0.1", port=port,
                              clock=clock)
        assert error.value.code == "BRIDGE_PORT_INVALID"
    for ttl in (4, 601, True, 1.5):
        with pytest.raises(module.BridgeError) as error:
            module.DemoBridge(token=TOKEN, tombstones_path=tmp_path / "t.jsonl",
                              kill_switch_store=store, host="127.0.0.1", port=0,
                              command_ttl_seconds=ttl, clock=clock)
        assert error.value.code == "BRIDGE_TTL_INVALID"
    for broken in (None, tmp_path / "kill_switch.json",
                   module.KillSwitchStore):
        # A bridge that cannot reach the durable stop is not allowed to start at all.
        with pytest.raises(module.BridgeError) as error:
            module.DemoBridge(token=TOKEN, tombstones_path=tmp_path / "t.jsonl",
                              kill_switch_store=broken, host="127.0.0.1", port=0, clock=clock)
        assert error.value.code == "KILL_SWITCH_STORE_REQUIRED"


def test_started_bridge_binds_loopback_and_reports_its_url(make_bridge):
    bridge = make_bridge()
    assert bridge.base_url == f"http://127.0.0.1:{bridge.port}"
    bridge.start()
    assert bridge.port > 0
    assert bridge.base_url == f"http://127.0.0.1:{bridge.port}"
    status, body, _ = _request(bridge, "GET", _bridge().KILL_SWITCH_PATH)
    assert status == 200
    # A switch nobody has ever written is a blocker, not a pass: absence is not consent.
    assert _json(body) == {"kill_switch_active": True, "kill_switch_reason": "NOT_SEEDED"}


# --------------------------------------------------------------------- validation
def test_validation_accepts_a_complete_command_and_drops_extra_fields(clock):
    module = _bridge()
    clean = module.validate_command(
        _command(clock()), now=clock().timestamp())
    assert set(clean) == set(module.REQUIRED_KEYS)
    padded = _command(clock(), extra="not part of the contract")
    assert "extra" not in module.validate_command(padded, now=clock().timestamp())


@pytest.mark.parametrize("overrides, code", [
    ({"mode": "live"}, "COMMAND_MODE_NOT_DEMO"),
    ({"mode": "DEMO"}, "COMMAND_MODE_NOT_DEMO"),
    ({"user_decision": "REJECT"}, "COMMAND_NOT_APPROVED"),
    ({"user_approved": False}, "COMMAND_NOT_APPROVED"),
    ({"user_approved": 1}, "COMMAND_NOT_APPROVED"),
    ({"direction": "buy"}, "COMMAND_DIRECTION_INVALID"),
    ({"direction": "SELL"}, None),
    ({"size": 0}, "COMMAND_NUMBER_INVALID"),
    ({"size": -0.01}, "COMMAND_NUMBER_INVALID"),
    ({"size": True}, "COMMAND_NUMBER_INVALID"),
    ({"expected_price": float("nan")}, "COMMAND_NUMBER_INVALID"),
    ({"sl": float("inf")}, "COMMAND_NUMBER_INVALID"),
    ({"command_id": "has space"}, "COMMAND_IDENTIFIER_INVALID"),
    ({"command_id": "x" * 129}, "COMMAND_IDENTIFIER_INVALID"),
    ({"instrument": "EUR/USD"}, "COMMAND_IDENTIFIER_INVALID"),
    ({"instrument": 'EUR"USD'}, "COMMAND_FIELD_INVALID"),
    ({"user_decision_id": "bad\\escape"}, "COMMAND_FIELD_INVALID"),
    ({"signal_id": "line\nbreak"}, "COMMAND_FIELD_INVALID"),
    ({"created_at": 17}, "COMMAND_FIELD_INVALID"),
    ({"payload_hash": "p" * 129}, "COMMAND_FIELD_INVALID"),
    ({"expires_at_epoch": True}, "COMMAND_EXPIRY_INVALID"),
    ({"expires_at_epoch": 0}, "COMMAND_EXPIRY_OUT_OF_RANGE"),
])
def test_validation_refuses_a_broken_command(clock, overrides, code):
    module = _bridge()
    if code is None:
        module.validate_command(_command(clock(), **overrides), now=clock().timestamp())
        return
    with pytest.raises(module.BridgeError) as error:
        module.validate_command(_command(clock(), **overrides), now=clock().timestamp())
    assert error.value.code == code


def test_validation_refuses_missing_and_non_flat_fields(clock):
    module = _bridge()
    incomplete = _command(clock())
    del incomplete["permission_id"]
    with pytest.raises(module.BridgeError) as error:
        module.validate_command(incomplete, now=clock().timestamp())
    assert error.value.code == "COMMAND_MISSING_FIELDS"
    nested = _command(clock(), permission_id={"nested": True})
    with pytest.raises(module.BridgeError) as error:
        module.validate_command(nested, now=clock().timestamp())
    assert error.value.code == "COMMAND_NOT_FLAT"
    listing = _command(clock(), signal_id=["sig"])
    with pytest.raises(module.BridgeError) as error:
        module.validate_command(listing, now=clock().timestamp())
    assert error.value.code == "COMMAND_NOT_FLAT"
    with pytest.raises(module.BridgeError) as error:
        module.validate_command(["not", "an", "object"], now=clock().timestamp())
    assert error.value.code == "COMMAND_NOT_OBJECT"


def test_expiry_must_be_in_the_near_future(clock):
    module = _bridge()
    with pytest.raises(module.BridgeError) as error:
        module.validate_command(_command(clock(), ttl=-1), now=clock().timestamp(),
                                ttl_limit=120)
    assert error.value.code == "COMMAND_EXPIRY_OUT_OF_RANGE"
    with pytest.raises(module.BridgeError) as error:
        module.validate_command(_command(clock(), ttl=121), now=clock().timestamp(),
                                ttl_limit=120)
    assert error.value.code == "COMMAND_EXPIRY_OUT_OF_RANGE"
    with pytest.raises(module.BridgeError) as error:
        module.validate_command(_command(clock(), ttl=601), now=clock().timestamp())
    assert error.value.code == "COMMAND_EXPIRY_OUT_OF_RANGE"


# ------------------------------------------------------------------------ serving
def test_a_command_is_served_exactly_once(make_bridge, clock):
    module = _bridge()
    bridge = make_bridge().start()
    bridge.enqueue(_command(clock()))
    status, body, headers = _request(bridge, "GET", module.NEXT_COMMAND_PATH)
    assert status == 200
    assert headers["cache-control"] == "no-store"
    served = _json(body)
    assert served["command_id"] == "cmd-1"
    assert served["client_order_id"] == "ord-1"
    assert TOKEN.encode() not in body
    assert bridge.served_at("cmd-1") is not None
    assert bridge.state.served == 1
    status, body, _ = _request(bridge, "GET", module.NEXT_COMMAND_PATH)
    assert status == 204
    assert body == b""


def test_only_one_command_may_be_pending(make_bridge, clock):
    module = _bridge()
    bridge = make_bridge().start()
    bridge.enqueue(_command(clock()))
    with pytest.raises(module.BridgeError) as error:
        bridge.enqueue(_command(clock(), command_id="cmd-2", client_order_id="ord-2"))
    assert error.value.code == "QUEUE_BUSY"
    assert bridge.state.reasons["QUEUE_BUSY"] == 1


def test_an_expired_command_is_dropped_and_may_be_requeued(make_bridge, clock):
    module = _bridge()
    bridge = make_bridge().start()
    bridge.enqueue(_command(clock()))
    clock.advance(61)
    status, body, _ = _request(bridge, "GET", module.NEXT_COMMAND_PATH)
    assert status == 204
    assert body == b""
    assert bridge.expired_at("cmd-1") is not None
    assert bridge.state.expired == 1
    # It never reached the EA, so the deterministic id must remain usable later.
    bridge.enqueue(_command(clock()))
    status, body, _ = _request(bridge, "GET", module.NEXT_COMMAND_PATH)
    assert status == 200
    assert _json(body)["command_id"] == "cmd-1"


def test_requeueing_a_served_command_is_refused(make_bridge, clock):
    module = _bridge()
    bridge = make_bridge().start()
    bridge.enqueue(_command(clock()))
    _request(bridge, "GET", module.NEXT_COMMAND_PATH)
    with pytest.raises(module.BridgeError) as error:
        bridge.enqueue(_command(clock()))
    assert error.value.code == "DUPLICATE_COMMAND"
    assert bridge.state.reasons["DUPLICATE_COMMAND"] == 1


def test_a_served_command_stays_served_after_a_restart(tmp_path, clock):
    module = _bridge()
    path = tmp_path / "tombstones.jsonl"
    store = module.KillSwitchStore(tmp_path / "kill_switch.json")
    first = module.DemoBridge(token=TOKEN, tombstones_path=path, kill_switch_store=store,
                              host="127.0.0.1", port=0, clock=clock).start()
    try:
        first.enqueue(_command(clock()))
        _request(first, "GET", module.NEXT_COMMAND_PATH)
    finally:
        first.stop()

    second = module.DemoBridge(token=TOKEN, tombstones_path=path,
                               kill_switch_store=module.KillSwitchStore(
                                   tmp_path / "kill_switch.json"),
                               host="127.0.0.1", port=0, clock=clock).start()
    try:
        with pytest.raises(module.BridgeError) as error:
            second.enqueue(_command(clock()))
        assert error.value.code == "DUPLICATE_COMMAND"
        # Also refused if only the order id collides with a new command id.
        with pytest.raises(module.BridgeError) as error:
            second.enqueue(_command(clock(), command_id="cmd-new"))
        assert error.value.code == "DUPLICATE_COMMAND"
        status, _, _ = _request(second, "GET", module.NEXT_COMMAND_PATH)
        assert status == 204
    finally:
        second.stop()


def test_an_unwritable_tombstone_fails_the_serve(make_bridge, clock):
    module = _bridge()
    bridge = make_bridge().start()
    bridge.enqueue(_command(clock()))

    def boom(*args, **kwargs):
        raise module.BridgeError("DISK_FULL")

    bridge._tombstones.add = boom
    try:
        status, body, _ = _request(bridge, "GET", module.NEXT_COMMAND_PATH)
        assert status == 503
        assert _json(body) == {"error": "tombstone_unavailable"}
        assert bridge.state.reasons["TOMBSTONE_WRITE_FAILED"] == 1
        # A second attempt while the record still cannot be written stays closed too.
        status, _, _ = _request(bridge, "GET", module.NEXT_COMMAND_PATH)
        assert status == 503
    finally:
        del bridge._tombstones.add
    # The command was not consumed and not tombstoned by the failed attempts.
    assert bridge.served_at("cmd-1") is None
    status, body, _ = _request(bridge, "GET", module.NEXT_COMMAND_PATH)
    assert status == 200
    assert _json(body)["command_id"] == "cmd-1"


# ------------------------------------------------------------------------- auth
def test_a_bad_or_missing_token_is_unauthorized(make_bridge, clock):
    module = _bridge()
    bridge = make_bridge().start()
    for key in (None, "", "b" * 43, TOKEN + "x"):
        status, body, _ = _request(bridge, "GET", module.KILL_SWITCH_PATH, key=key)
        assert status == 401
        assert _json(body) == {"error": "unauthorized"}
    bridge.enqueue(_command(clock()))
    status, _, _ = _request(bridge, "GET", module.NEXT_COMMAND_PATH, key=None)
    assert status == 401
    # The unauthorized read must not have consumed the command.
    status, body, _ = _request(bridge, "GET", module.NEXT_COMMAND_PATH)
    assert status == 200


def test_a_foreign_host_header_is_refused(make_bridge):
    module = _bridge()
    bridge = make_bridge().start()
    for host in ("evil.example:8000", "evil.example", f"192.168.1.5:{bridge.port}"):
        status, body, _ = _request(bridge, "GET", module.KILL_SWITCH_PATH,
                                   headers={"Host": host})
        assert status == 403
        assert _json(body) == {"error": "host_not_allowed"}
    for host in (f"localhost:{bridge.port}", "localhost", "127.0.0.1", f"127.0.0.1:{bridge.port}"):
        status, _, _ = _request(bridge, "GET", module.KILL_SWITCH_PATH,
                                headers={"Host": host})
        assert status == 200


def test_mql5_bare_host_header_is_accepted(make_bridge):
    """MQL5 WebRequest sends ``Host: 127.0.0.1`` - the bare address, no port.

    The bridge refused that as host_not_allowed, so every EA poll was a 403 while a
    browser or a script that fills in the port was served. A bare loopback name is the
    same host, not a foreign one; a foreign name is still refused.
    """
    module = _bridge()
    bridge = make_bridge().start()
    status, _, _ = _request(bridge, "GET", module.KILL_SWITCH_PATH,
                            headers={"Host": "127.0.0.1"})
    assert status == 200
    status, body, _ = _request(bridge, "GET", module.KILL_SWITCH_PATH,
                               headers={"Host": "evil.example"})
    assert status == 403 and _json(body) == {"error": "host_not_allowed"}


def test_a_request_without_a_host_header_is_served(make_bridge):
    """MQL5 WebRequest sends no Host header; the loopback socket is the trust boundary.

    A browser cannot omit Host, so the DNS-rebinding refusal is untouched by this
    allowance - only a client that speaks HTTP itself, on the loopback socket, can
    arrive without one.
    """
    module = _bridge()
    bridge = make_bridge().start()
    request = f"GET {module.KILL_SWITCH_PATH} HTTP/1.1\r\nX-Bridge-Key: {TOKEN}\r\n\r\n"
    with socket.create_connection(("127.0.0.1", bridge.port), timeout=5) as sock:
        sock.sendall(request.encode("ascii"))
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    assert b" 200 " in head
    payload = _json(body)
    # An unseeded store answers with the stop armed; the point here is that the request
    # was served at all instead of being refused as host_not_allowed.
    assert "kill_switch_active" in payload


def test_an_origin_header_is_refused(make_bridge):
    module = _bridge()
    bridge = make_bridge().start()
    status, body, _ = _request(bridge, "GET", module.KILL_SWITCH_PATH,
                               headers={"Origin": "http://evil.example"})
    assert status == 401
    assert _json(body) == {"error": "unauthorized"}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_write_verbs_are_rejected(make_bridge, method):
    module = _bridge()
    bridge = make_bridge().start()
    status, body, _ = _request(bridge, method, module.NEXT_COMMAND_PATH)
    assert status == 405
    assert _json(body) == {"error": "method_not_allowed"}


def test_unknown_paths_return_404(make_bridge):
    bridge = make_bridge().start()
    for path in ("/", "/v1/execution/other", "/v1/execution/next/../next"):
        status, body, _ = _request(bridge, "GET", path)
        assert status == 404
        assert _json(body) == {"error": "not_found"}


def test_a_query_string_does_not_change_the_route(make_bridge, clock):
    module = _bridge()
    bridge = make_bridge().start()
    bridge.enqueue(_command(clock()))
    log: list[str] = []
    bridge._debug_log = log.append
    status, body, _ = _request(bridge, "GET", module.NEXT_COMMAND_PATH + "?probe=secret")
    assert status == 200
    assert _json(body)["command_id"] == "cmd-1"
    assert log, "nothing was logged, so the assertion below proves nothing"
    assert all("probe" not in line and "secret" not in line and TOKEN not in line
               for line in log)


# ------------------------------------------------------------------ kill switch
def test_kill_switch_payload_is_served(make_bridge, tmp_path, clock):
    module = _bridge()
    store = _arm(module.KillSwitchStore(tmp_path / "armed.json"), reason="operator")
    bridge = make_bridge(kill_switch_store=store).start()
    status, body, headers = _request(bridge, "GET", module.KILL_SWITCH_PATH)
    assert status == 200
    assert headers["cache-control"] == "no-store"
    assert _json(body) == {"kill_switch_active": True, "kill_switch_reason": "operator"}


def test_an_unreadable_state_is_never_served_as_disarmed(make_bridge, tmp_path):
    module = _bridge()
    path = tmp_path / "kill_switch.json"
    store = module.KillSwitchStore(path)
    bridge = make_bridge(kill_switch_store=store).start()
    for broken in ("", "not json at all", "[]", '{"kill_switch_active": "yes"}',
                   '{"kill_switch_active": true, "kill_switch_reason": {"x": 1}}'):
        path.write_text(broken, encoding="utf-8")
        status, body, _ = _request(bridge, "GET", module.KILL_SWITCH_PATH)
        assert status == 503, broken
        assert _json(body) == {"error": "kill_switch_unavailable"}


def test_kill_switch_strings_are_made_parseable(make_bridge, tmp_path):
    """A hand-edited reason must still parse in the advisor's strict JSON reader."""
    module = _bridge()
    path = tmp_path / "kill_switch.json"
    path.write_text(json.dumps({"kill_switch_active": False,
                                "kill_switch_reason": 'operator said "stop"\\now\nimmediately',
                                "actor_id": "hand", "updated_at": "2026-10-31T12:00:00+00:00",
                                "event_hash": "0" * 64}), encoding="utf-8")
    bridge = make_bridge(kill_switch_store=module.KillSwitchStore(path)).start()
    status, body, _ = _request(bridge, "GET", module.KILL_SWITCH_PATH)
    assert status == 200
    payload = _json(body)
    assert payload["kill_switch_active"] is False
    reason = payload["kill_switch_reason"]
    assert '"' not in reason and "\\" not in reason
    assert not any(ord(char) < 0x20 for char in reason)


# --------------------------------------------------------------------- stop write
def test_an_authorized_post_arms_the_stop_the_ea_reads(make_bridge, clock):
    module = _bridge()
    bridge = make_bridge().start()
    status, body, _ = _post(bridge, module.KILL_SWITCH_PATH,
                            {"active": True, "actor_id": "paula", "reason": "circuit breaker"})
    assert status == 200
    record = _json(body)
    assert record["kill_switch_active"] is True
    assert record["status"] == "PERSISTED"
    assert record["actor_id"] == "paula"
    assert len(record["event_hash"]) == 64
    assert datetime.fromisoformat(record["updated_at"]) == clock()
    # The same state the advisor polls, not a second copy that can drift.
    status, polled, _ = _request(bridge, "GET", module.KILL_SWITCH_PATH)
    assert _json(polled) == {"kill_switch_active": True,
                             "kill_switch_reason": "circuit breaker"}


def test_the_stop_is_durable_and_outlives_the_bridge(tmp_path, clock):
    module = _bridge()
    path = tmp_path / "kill_switch.json"
    store = module.KillSwitchStore(path)
    bridge = module.DemoBridge(token=TOKEN, tombstones_path=tmp_path / "tombstones.jsonl",
                               kill_switch_store=store, host="127.0.0.1", port=0,
                               clock=clock).start()
    try:
        _post(bridge, module.KILL_SWITCH_PATH, {"active": True, "reason": "crash risk"})
    finally:
        bridge.stop()
    # A new process reading the file is blocked too: the stop was never a variable.
    reopened = module.DemoBridge(token=TOKEN, tombstones_path=tmp_path / "tombstones.jsonl",
                                 kill_switch_store=module.KillSwitchStore(path),
                                 host="127.0.0.1", port=0, clock=clock).start()
    try:
        status, body, _ = _request(reopened, "GET", module.KILL_SWITCH_PATH)
        assert _json(body) == {"kill_switch_active": True, "kill_switch_reason": "crash risk"}
        assert "crash risk" in path.read_text(encoding="utf-8")
    finally:
        reopened.stop()


def test_a_stop_request_defaults_to_armed_and_survives_awkward_wording(make_bridge):
    module = _bridge()
    bridge = make_bridge().start()
    status, body, _ = _post(bridge, module.KILL_SWITCH_PATH, {})
    assert status == 200 and _json(body)["kill_switch_active"] is True
    # An operator's wording is prose; the advisor's parser is strict. Clean the text,
    # never refuse the stop over it.
    status, body, _ = _post(bridge, module.KILL_SWITCH_PATH,
                            {"reason": 'скажу "стоп"\\и ещё\nперенос', "actor_id": "x" * 400})
    assert status == 200
    record = _json(body)
    assert '"' not in record["kill_switch_reason"] and "\\" not in record["kill_switch_reason"]
    assert len(record["actor_id"]) <= module.MAX_NOTE_CHARS
    status, polled, _ = _request(bridge, "GET", module.KILL_SWITCH_PATH)
    assert status == 200


@pytest.mark.parametrize(("raw", "code"), [
    ('{"active": "true"}', "active_is_not_true_or_false"),
    ('{"active": 1}', "active_is_not_true_or_false"),
    ('{"reason": 5}', "note_is_not_a_string"),
    ('{"actor_id": []}', "note_is_not_a_string"),
    ("[]", "body_is_not_a_json_object"),
    ("not json", "body_is_not_a_json_object"),
])
def test_a_malformed_stop_request_changes_nothing(make_bridge, raw, code):
    module = _bridge()
    bridge = make_bridge().start()
    status, payload, _ = _post(bridge, module.KILL_SWITCH_PATH, None, raw=raw)
    assert status == 400
    assert _json(payload)["error"] == code
    # An unusable request never releases a stop, and never arms one either.
    assert bridge._switch.read() is None


def test_an_empty_or_huge_stop_body_is_refused_after_being_read(make_bridge):
    module = _bridge()
    bridge = make_bridge().start()
    # Nothing in the body, so nothing that could be a stop; and a body far past the cap is
    # drained out of the socket before the 413, so the client learns the verdict instead of
    # losing the connection to a reset.
    status, body, _ = _request(bridge, "POST", module.KILL_SWITCH_PATH)
    assert status == 400 and _json(body) == {"error": "body_required"}
    status, body, _ = _request(bridge, "POST", module.KILL_SWITCH_PATH, body="{}" * 4000)
    assert status == 413 and _json(body) == {"error": "body_too_large"}
    assert bridge._switch.read() is None


def _headers(bridge, method: str, path: str, *, length: int) -> bytes:
    """A hand-written request line, so a test can control when the body bytes arrive."""
    return (f"{method} {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{bridge.port}\r\n"
            f"X-Bridge-Key: {TOKEN}\r\n"
            f"Content-Length: {length}\r\n\r\n").encode("ascii")


def test_a_refusal_waits_for_the_body_the_request_declared(make_bridge):
    """The verdict arrives only after the declared bytes have left the client.

    An answer written over a body still in flight is a reset, not an answer, and an
    operator whose stop request dies on a reset has no way to tell whether the stop
    happened. So the declared length is drained first, however long it is.
    """
    module = _bridge()
    bridge = make_bridge().start()
    body = b"{" * 8000  # past the cap, so the bridge must read it all before answering
    connection = socket.create_connection(("127.0.0.1", bridge.port), timeout=5)
    try:
        connection.sendall(_headers(bridge, "POST", module.KILL_SWITCH_PATH, length=len(body)))
        connection.sendall(body[:4000])
        readable, _, _ = select.select([connection], [], [], 0.3)
        assert not readable, "the bridge answered while the client was still sending"
        connection.sendall(body[4000:])
        reply = bytearray()
        while b"\r\n\r\n" not in reply:
            piece = connection.recv(4096)
            if not piece:
                break
            reply += piece
    finally:
        connection.close()
    assert b" 413 " in reply, bytes(reply)
    assert bridge._switch.read() is None


def test_a_client_that_stops_mid_request_is_dropped_not_waited_on(make_bridge, monkeypatch):
    """A stalled client cannot pin a handler thread, because every socket read is bounded.

    The body is drained before any answer, which is also the moment a client that never
    finishes sending could hold the bridge open for good.
    """
    module = _bridge()
    monkeypatch.setattr(module, "REQUEST_TIMEOUT_SECONDS", 0.5)
    bridge = make_bridge().start()
    connection = socket.create_connection(("127.0.0.1", bridge.port), timeout=5)
    try:
        connection.sendall(_headers(bridge, "POST", module.KILL_SWITCH_PATH, length=4096))
        connection.sendall(b"{" * 10)
        assert connection.recv(1) == b"", "the bridge kept the stalled connection open"
    finally:
        connection.close()
    assert bridge._switch.read() is None


def test_the_stop_needs_the_bridge_key_and_a_loopback_host(make_bridge):
    module = _bridge()
    bridge = make_bridge().start()
    for key in (None, "", "b" * 43):
        status, body, _ = _post(bridge, module.KILL_SWITCH_PATH, {"active": True}, key=key)
        assert status == 401 and _json(body) == {"error": "unauthorized"}
    status, body, _ = _post(bridge, module.KILL_SWITCH_PATH, {"active": True},
                            headers={"Origin": "http://evil.example"})
    assert status == 401
    assert bridge._switch.read() is None
    # A cross-site host header is refused before the stop is even attempted.
    status, body, _ = _post(bridge, module.KILL_SWITCH_PATH, {"active": True},
                            headers={"Host": "localhost:1"})
    assert status == 403 and _json(body) == {"error": "host_not_allowed"}


def test_only_the_kill_switch_path_accepts_a_write(make_bridge, clock):
    module = _bridge()
    bridge = make_bridge().start()
    bridge.enqueue(_command(clock()))
    for path in (module.NEXT_COMMAND_PATH, "/v1/execution/submit", "/unknown"):
        status, body, _ = _post(bridge, path, {"active": True})
        assert status == 405, path
        assert _json(body) == {"error": "method_not_allowed"}
    # A queue with a command in it is still untouchable from the outside.
    status, body, _ = _request(bridge, "GET", module.NEXT_COMMAND_PATH)
    assert status == 200 and _json(body)["command_id"] == "cmd-1"
    for verb in ("PUT", "PATCH", "DELETE"):
        status, _, _ = _request(bridge, verb, module.KILL_SWITCH_PATH, body="{}")
        assert status == 405, verb


def test_releasing_a_stop_needs_a_second_secret(make_bridge):
    module = _bridge()
    secret = "a" * 24
    bridge = make_bridge(admin_token=lambda: secret).start()
    _post(bridge, module.KILL_SWITCH_PATH, {"active": True, "reason": "armed"})
    # Misconfigured: the release is refused and the switch stays armed.
    status, body, _ = _post(bridge, module.KILL_SWITCH_PATH, {"active": False})
    assert status == 503
    assert _json(body) == {"error": "KILL_SWITCH_ADMIN_TOKEN_UNCONFIGURED",
                           "kill_switch_active": True}
    bridge_same_secret = make_bridge(admin_token=lambda: TOKEN, token=TOKEN).start()
    status, body, _ = _post(bridge_same_secret, module.KILL_SWITCH_PATH, {"active": False})
    assert status == 503
    assert "REUSES_BRIDGE_TOKEN" in _json(body)["error"]
    bridge._admin_token = lambda: "b" * 32
    status, body, _ = _post(bridge, module.KILL_SWITCH_PATH, {"active": False})
    assert status == 403
    assert _json(body) == {"error": "KILL_SWITCH_ADMIN_AUTHENTICATION_REQUIRED",
                           "kill_switch_active": True}
    status, body, _ = _post(bridge, module.KILL_SWITCH_PATH, {"active": False},
                            headers={module.ADMIN_HEADER: "wrong" * 20})
    assert status == 403
    status, body, _ = _post(bridge, module.KILL_SWITCH_PATH, {"active": False},
                            headers={module.ADMIN_HEADER: "b" * 32})
    assert status == 200 and _json(body)["kill_switch_active"] is False
    status, polled, _ = _request(bridge, "GET", module.KILL_SWITCH_PATH)
    assert _json(polled)["kill_switch_active"] is False


def test_an_arm_that_cannot_be_written_is_still_an_arm(make_bridge, monkeypatch):
    module = _bridge()
    bridge = make_bridge().start()
    monkeypatch.setattr(module.KillSwitchStore, "_write",
                        lambda self, record: (_ for _ in ()).throw(OSError("disk full")))
    status, body, _ = _post(bridge, module.KILL_SWITCH_PATH, {"active": True,
                                                              "reason": "disk problem"})
    assert status == 503
    assert _json(body) == {"kill_switch_active": True, "kill_switch_reason": "disk problem",
                           "error": "OSError", "status": "MEMORY_ONLY"}
    # The EA still reads the stop: an unwritable file must not un-arm the process.
    status, polled, _ = _request(bridge, "GET", module.KILL_SWITCH_PATH)
    assert _json(polled)["kill_switch_active"] is True
    assert bridge.state.reasons["OSError"] == 1


def test_a_release_that_cannot_be_written_leaves_the_stop_armed(make_bridge, monkeypatch):
    module = _bridge()
    secret = "b" * 32
    bridge = make_bridge(admin_token=lambda: secret).start()
    _post(bridge, module.KILL_SWITCH_PATH, {"active": True, "reason": "armed"})
    monkeypatch.setattr(module.KillSwitchStore, "_write",
                        lambda self, record: (_ for _ in ()).throw(OSError("disk full")))
    status, body, _ = _post(bridge, module.KILL_SWITCH_PATH, {"active": False},
                            headers={module.ADMIN_HEADER: secret})
    assert status == 503
    assert _json(body)["status"] == "NOT_CHANGED"
    status, polled, _ = _request(bridge, "GET", module.KILL_SWITCH_PATH)
    assert _json(polled)["kill_switch_active"] is True


def test_stop_state_round_trips_through_the_file(tmp_path, clock):
    module = _bridge()
    path = tmp_path / "kill_switch.json"
    store = module.KillSwitchStore(path)
    assert store.read() is None
    assert store.payload() == {"kill_switch_active": True, "kill_switch_reason": "NOT_SEEDED"}
    first = store.record(active=True, actor_id="operator", reason="risk", now=clock())
    assert _json(path.read_bytes())["kill_switch_active"] is True
    second = module.KillSwitchStore(path)
    assert second.payload() == {"kill_switch_active": True, "kill_switch_reason": "risk"}
    assert second.read()["event_hash"] == first["event_hash"]
    second.record(active=False, actor_id="admin", reason="checked", now=clock())
    assert module.KillSwitchStore(path).payload()["kill_switch_active"] is False


def test_a_record_that_was_written_and_then_deleted_is_a_refusal(tmp_path, clock):
    """Deleting the state file cannot be a way to disarm anything.

    A process that has seen a record treats its disappearance as corruption, and a process
    that never saw one reads an armed switch, so neither direction ends in trading.
    """
    module = _bridge()
    path = tmp_path / "kill_switch.json"
    store = module.KillSwitchStore(path)
    store.record(active=False, actor_id="admin", reason="checked", now=clock())
    path.unlink()
    with pytest.raises(module.BridgeError) as error:
        store.read()
    assert error.value.code == "KILL_SWITCH_FILE_CORRUPT"
    assert "removed" in str(error.value)
    # And a brand-new process reading the same absent path is blocked, not released.
    assert module.KillSwitchStore(path).payload()["kill_switch_active"] is True


def test_a_stop_released_elsewhere_is_seen_without_a_restart(tmp_path, clock):
    """The file is the authority on every read, so a release is not trapped in one process.

    The opposite half is already covered: an arm whose write failed stays in memory, because
    losing a stop the operator just pulled is worse than holding one too long.
    """
    module = _bridge()
    path = tmp_path / "kill_switch.json"
    watcher = module.KillSwitchStore(path)
    watcher.record(active=True, actor_id="operator", reason="circuit breaker", now=clock())
    assert watcher.payload()["kill_switch_active"] is True
    module.KillSwitchStore(path).record(active=False, actor_id="admin", reason="checked",
                                        now=clock())
    assert watcher.payload() == {"kill_switch_active": False, "kill_switch_reason": "checked"}


def test_each_switch_write_gets_its_own_temporary_file(tmp_path, clock, monkeypatch):
    """Two stores on one path must not meet on one scratch name.

    A fixed ``.tmp`` name is a rendezvous: whichever process renames first hands the other's
    half-written body to the state path, which is exactly the corruption the file is supposed
    to make loud rather than cause.
    """
    module = _bridge()
    path = tmp_path / "kill_switch.json"
    store = module.KillSwitchStore(path)
    names: list[str] = []
    real_mkstemp = module.tempfile.mkstemp

    def spy(**kwargs):
        descriptor, name = real_mkstemp(**kwargs)
        names.append(name)
        return descriptor, name

    monkeypatch.setattr(module.tempfile, "mkstemp", spy)
    store.record(active=True, actor_id="operator", reason="first", now=clock())
    store.record(active=False, actor_id="admin", reason="second", now=clock())
    assert len(names) == 2 and len(set(names)) == 2
    assert all(Path(name).parent == path.parent for name in names)
    assert not [leftover for leftover in path.parent.iterdir() if leftover != path]
    assert json.loads(path.read_text(encoding="utf-8"))["kill_switch_reason"] == "second"


def test_a_failed_switch_rename_leaves_the_old_record_and_no_scratch(tmp_path, clock,
                                                                     monkeypatch):
    module = _bridge()
    path = tmp_path / "kill_switch.json"
    store = module.KillSwitchStore(path)
    store.record(active=False, actor_id="admin", reason="checked", now=clock())

    def refuse(*_args, **_kwargs):
        raise OSError("rename blocked")

    monkeypatch.setattr(module.os, "replace", refuse)
    with pytest.raises(OSError):
        store.record(active=True, actor_id="operator", reason="breaker", now=clock())
    assert json.loads(path.read_text(encoding="utf-8"))["kill_switch_reason"] == "checked"
    assert not [leftover for leftover in path.parent.iterdir() if leftover != path]
    # The arm still holds in this process even though the rename failed.
    assert store.payload()["kill_switch_active"] is True


@pytest.mark.parametrize("reason, expected", [
    ('quoted "stop" and \\backslash', "quoted stop and backslash"),
    ("line\nbreak\tand tab", "line break and tab"),
    ("   padded   ", "padded"),
    ("", "ARMED"),
    (None, "ARMED"),
    ({"nested": 1}, "ARMED"),
    ("x" * 400, "x" * 180),
])
def test_a_stop_note_is_cleaned_not_refused(tmp_path, clock, reason, expected):
    module = _bridge()
    record = module.KillSwitchStore(tmp_path / "kill_switch.json").record(
        active=True, actor_id="operator", reason=reason, now=clock())
    assert record["kill_switch_reason"] == expected
    assert '"' not in record["kill_switch_reason"] and "\\" not in record["kill_switch_reason"]
    assert not any(ord(char) < 0x20 for char in record["kill_switch_reason"])


def test_a_corrupt_stop_file_is_a_refusal_not_a_default(tmp_path):
    module = _bridge()
    path = tmp_path / "kill_switch.json"
    for broken in ("", "   ", "not json at all", "[]", '{"kill_switch_active": "yes"}',
                   '{"kill_switch_active": true}'):
        path.write_text(broken, encoding="utf-8")
        with pytest.raises(module.BridgeError) as error:
            module.KillSwitchStore(path)
        assert error.value.code == "KILL_SWITCH_FILE_CORRUPT", broken


# ------------------------------------------------------------------- tombstones
def test_tombstone_store_round_trips_and_refuses_duplicates(tmp_path, clock):
    module = _bridge()
    path = tmp_path / "tombstones.jsonl"
    store = module.TombstoneStore(path)
    store.add("cmd-1", "ord-1", now=clock())
    assert store.contains("cmd-1", "ord-1")
    assert store.contains("cmd-1", "other")
    assert store.contains("other", "ord-1")
    with pytest.raises(module.BridgeError) as error:
        store.add("cmd-1", "ord-2", now=clock())
    assert error.value.code == "DUPLICATE_COMMAND"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["client_order_id"] == "ord-1"


def test_tombstone_store_ignores_only_a_torn_final_line(tmp_path, clock):
    module = _bridge()
    path = tmp_path / "tombstones.jsonl"
    good = json.dumps({"command_id": "cmd-1", "client_order_id": "ord-1",
                       "at": clock().isoformat()})
    path.write_text(good + "\n" + '{"command_id": "cmd-2", "client_ord', encoding="utf-8")
    store = module.TombstoneStore(path)
    assert store.contains("cmd-1", "ord-1")
    assert not store.contains("cmd-2", "ord-2")


def test_tombstone_store_refuses_other_corruption(tmp_path):
    module = _bridge()
    path = tmp_path / "tombstones.jsonl"
    path.write_text('{"command_id": "cmd-1"}\n', encoding="utf-8")
    with pytest.raises(module.BridgeError) as error:
        module.TombstoneStore(path)
    assert error.value.code == "TOMBSTONE_FILE_CORRUPT"
    path.write_text("not json\n" + '{"command_id": "cmd-1", "client_order_id": "ord-1"}\n',
                    encoding="utf-8")
    with pytest.raises(module.BridgeError) as error:
        module.TombstoneStore(path)
    assert error.value.code == "TOMBSTONE_FILE_CORRUPT"


def test_sha256_text_is_stable():
    module = _bridge()
    assert module.sha256_text("abc") == module.sha256_text("abc")
    assert len(module.sha256_text("abc")) == 64
    assert module.sha256_text("abc") != module.sha256_text("abd")


@pytest.mark.parametrize("kind", [ConnectionAbortedError, ConnectionResetError, BrokenPipeError])
def test_a_client_that_disconnects_mid_answer_costs_one_line_not_a_traceback(
        make_bridge, capsys, kind):
    # The terminal aborts its own WebRequest the moment it refuses the poll, so the bridge
    # regularly loses the socket with the response body still in flight. On 2026-11-02 six of
    # those came through socketserver as full tracebacks in the runner log, where they read as
    # the bridge crashing while the queue kept serving.
    bridge = make_bridge().start()
    try:
        raise kind(10053, "aborted by host software")
    except kind:
        bridge._server.handle_error(None, ("127.0.0.1", 50757))
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "closed the socket mid-request" in err
    assert "127.0.0.1:50757" in err


def test_an_unexpected_handler_error_still_gets_the_full_report(make_bridge, capsys):
    # Filtering by exception type is the whole point: a genuine bug on the bridge must not be
    # reduced to the same one line a client disconnect gets.
    bridge = make_bridge().start()
    try:
        raise ValueError("not a disconnect")
    except ValueError:
        bridge._server.handle_error(None, ("127.0.0.1", 50758))
    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert "ValueError: not a disconnect" in err


def test_an_authenticated_poll_on_an_ea_path_is_counted(make_bridge, clock):
    # The advisor logs only refusals and events, so a quiet log cannot tell a live poll loop from
    # a detached EA. The counter here is the positive record the transport check reads.
    bridge = make_bridge().start()
    assert bridge.state.ea_polls == 0
    _request(bridge, "GET", "/v1/risk/kill-switch")
    _request(bridge, "GET", "/v1/execution/next")
    status, body, _ = _request(bridge, "GET", "/v1/health")
    assert status == 200
    payload = _json(body)
    assert payload["ea_polls"] == 2
    assert payload["last_ea_poll_path"] == "/v1/execution/next"
    assert payload["ea_poll_age_seconds"] == 0.0
    clock.advance(45)
    assert _json(_request(bridge, "GET", "/v1/health")[1])["ea_poll_age_seconds"] == 45.0


def test_the_health_read_itself_is_not_an_advisor_poll_and_needs_the_token(make_bridge):
    bridge = make_bridge().start()
    _request(bridge, "GET", "/v1/risk/kill-switch")
    polls = bridge.state.ea_polls
    _request(bridge, "GET", "/v1/health")
    assert bridge.state.ea_polls == polls
    status, body, _ = _request(bridge, "GET", "/v1/health", key=None)
    assert status == 401
    status, _, _ = _request(bridge, "GET", "/v1/health", key="b" * 43)
    assert status == 401
    # A command that expires unserved still proves the advisor was polling for it, and the
    # health answer must not carry what it would have been handed.
    bridge.enqueue(_command(clock_now(bridge)))
    served = _json(_request(bridge, "GET", "/v1/execution/next")[1])
    health = json.dumps(_json(_request(bridge, "GET", "/v1/health")[1]))
    assert served["command_id"] not in health
    assert TOKEN not in health
    assert "payload_hash" not in health


def test_a_refused_poll_is_not_counted(make_bridge):
    bridge = make_bridge().start()
    _request(bridge, "GET", "/v1/risk/kill-switch", key="b" * 43)
    _post(bridge, "/v1/risk/kill-switch", {"action": "arm", "reason": "x", "actor_id": "y"},
          key="b" * 43)
    assert bridge.state.ea_polls == 0
    # An unauthenticated read of the health endpoint is not an advisor poll either, and the
    # queue path a browser could reach with an Origin header is refused before any counting.
    _request(bridge, "GET", "/v1/health", key=None)
    _request(bridge, "GET", "/v1/risk/kill-switch", headers={"Origin": "http://evil.example"})
    assert bridge.state.ea_polls == 0


def clock_now(bridge) -> datetime:
    return bridge._now()
