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
             headers: dict | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=5)
    request_headers = {} if headers is None else dict(headers)
    if key is not None:
        request_headers["X-Bridge-Key"] = key
    connection.request(method, path, headers=request_headers)
    response = connection.getresponse()
    body = response.read()
    status = response.status
    response_headers = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    return status, body, response_headers


def _json(body: bytes):
    return json.loads(body.decode("utf-8"))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def make_bridge(tmp_path, clock):
    created = []

    def factory(**kwargs):
        options = {
            "token": TOKEN,
            "tombstones_path": tmp_path / "tombstones.jsonl",
            "kill_switch": lambda: {"kill_switch_active": False, "kill_switch_reason": ""},
            "host": "127.0.0.1",
            "port": 0,
            "command_ttl_seconds": 120,
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
    for host in ("0.0.0.0", "::", "10.0.0.5", "example.com"):
        with pytest.raises(module.BridgeError) as error:
            module.DemoBridge(token=TOKEN, tombstones_path=tmp_path / "t.jsonl",
                              kill_switch=lambda: {"kill_switch_active": False},
                              host=host, port=0, clock=clock)
        assert error.value.code == "BRIDGE_HOST_NOT_LOOPBACK"
    for port in (-1, 70000, True, "8000"):
        with pytest.raises(module.BridgeError) as error:
            module.DemoBridge(token=TOKEN, tombstones_path=tmp_path / "t.jsonl",
                              kill_switch=lambda: {"kill_switch_active": False},
                              host="127.0.0.1", port=port, clock=clock)
        assert error.value.code == "BRIDGE_PORT_INVALID"
    for ttl in (4, 601, True, 1.5):
        with pytest.raises(module.BridgeError) as error:
            module.DemoBridge(token=TOKEN, tombstones_path=tmp_path / "t.jsonl",
                              kill_switch=lambda: {"kill_switch_active": False},
                              host="127.0.0.1", port=0, command_ttl_seconds=ttl, clock=clock)
        assert error.value.code == "BRIDGE_TTL_INVALID"


def test_started_bridge_binds_loopback_and_reports_its_url(make_bridge):
    bridge = make_bridge()
    assert bridge.base_url == f"http://127.0.0.1:{bridge.port}"
    bridge.start()
    assert bridge.port > 0
    assert bridge.base_url == f"http://127.0.0.1:{bridge.port}"
    status, body, _ = _request(bridge, "GET", _bridge().KILL_SWITCH_PATH)
    assert status == 200
    assert _json(body)["kill_switch_active"] is False


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
    first = module.DemoBridge(token=TOKEN, tombstones_path=path,
                              kill_switch=lambda: {"kill_switch_active": False},
                              host="127.0.0.1", port=0, clock=clock).start()
    try:
        first.enqueue(_command(clock()))
        _request(first, "GET", module.NEXT_COMMAND_PATH)
    finally:
        first.stop()

    second = module.DemoBridge(token=TOKEN, tombstones_path=path,
                               kill_switch=lambda: {"kill_switch_active": False},
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
    for host in ("evil.example:8000", "127.0.0.1", f"127.0.0.1:{bridge.port + 1}"):
        status, body, _ = _request(bridge, "GET", module.KILL_SWITCH_PATH,
                                   headers={"Host": host})
        assert status == 403
        assert _json(body) == {"error": "host_not_allowed"}
    status, _, _ = _request(bridge, "GET", module.KILL_SWITCH_PATH,
                            headers={"Host": f"localhost:{bridge.port}"})
    assert status == 200


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
    assert all("probe" not in line and "secret" not in line and TOKEN not in line
               for line in log)


# ------------------------------------------------------------------ kill switch
def test_kill_switch_payload_is_served(make_bridge):
    module = _bridge()
    bridge = make_bridge(
        kill_switch=lambda: {"kill_switch_active": True, "kill_switch_reason": "operator"}).start()
    status, body, headers = _request(bridge, "GET", module.KILL_SWITCH_PATH)
    assert status == 200
    assert headers["cache-control"] == "no-store"
    assert _json(body) == {"kill_switch_active": True, "kill_switch_reason": "operator"}


def test_kill_switch_failures_are_503(make_bridge):
    module = _bridge()

    def explode():
        raise RuntimeError("journal unavailable")

    bridge = make_bridge(kill_switch=explode).start()
    status, body, _ = _request(bridge, "GET", module.KILL_SWITCH_PATH)
    assert status == 503
    assert _json(body) == {"error": "kill_switch_unavailable"}

    bad_bridge = make_bridge(kill_switch=lambda: {"kill_switch_active": "yes"}).start()
    status, body, _ = _request(bad_bridge, "GET", module.KILL_SWITCH_PATH)
    assert status == 503

    nested = make_bridge(
        kill_switch=lambda: {"kill_switch_active": False, "detail": {"x": 1}}).start()
    status, _, _ = _request(nested, "GET", module.KILL_SWITCH_PATH)
    assert status == 503


def test_kill_switch_strings_are_made_parseable(make_bridge):
    module = _bridge()
    bridge = make_bridge(kill_switch=lambda: {
        "kill_switch_active": False,
        "kill_switch_reason": 'operator said "stop"\\now\nimmediately',
    }).start()
    status, body, _ = _request(bridge, "GET", module.KILL_SWITCH_PATH)
    assert status == 200
    payload = _json(body)
    assert payload["kill_switch_active"] is False
    reason = payload["kill_switch_reason"]
    assert '"' not in reason and "\\" not in reason
    assert not any(ord(char) < 0x20 for char in reason)


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
