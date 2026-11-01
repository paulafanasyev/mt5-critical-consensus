"""The MQL5 EA and the Python side must not grow separate ideas of the same rule.

``scripts/ea_logic_mirror.py`` is a line-for-line port of the EA's parser, identifier rules,
tombstone hash, URL rule and command gate. These tests run that port against what this repo's
Python actually produces - the bodies a live bridge writes to the socket, the command the runner
builds, the ceilings and key lists each side declares - and require the two to agree. A message
reworded in the EA source, a ceiling raised on one side only, or a field the EA starts to require
fails a test here instead of showing up as an unexplained ``BLOCKED`` line on a chart.
"""
from __future__ import annotations

import http.client
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ea_logic_mirror as ea  # noqa: E402
import run_mt5_demo_trading as runner  # noqa: E402
from mt5_demo_bridge import (IDENTIFIER_PATTERN, KILL_SWITCH_PATH, MAX_TTL_SECONDS,  # noqa: E402
                             REQUIRED_KEYS, TOKEN_PATTERN, DemoBridge, KillSwitchStore)

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
NOW_EPOCH = NOW.timestamp()
TOKEN = "a" * 43


def get_switch_body(port: int) -> tuple[int, str]:
    """One EA-shaped poll: no Host header, because MQL5 sends only the headers it is given."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", KILL_SWITCH_PATH, headers={"X-Bridge-Key": TOKEN})
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8")
    finally:
        connection.close()


def command_body(**overrides) -> str:
    """A command exactly as the runner builds it, serialized exactly as the bridge writes it."""
    fields = {"command_id": "cmd-1", "client_order_id": "ord-1", "permission_id": "perm-1",
              "signal_id": "sig-1", "instrument": "XAUUSD", "direction": "BUY", "size": 0.20,
              "expected_price": 4000.0, "sl": 3990.0, "tp": 4020.0,
              "user_decision_id": "dec-1"}
    moment = overrides.pop("now", NOW)
    ttl = overrides.pop("ttl_seconds", 60)
    payload = runner.build_command(**fields, now=moment, ttl_seconds=ttl)
    # The EA reads these literals and the runner fixes them, so breaking one means editing the
    # body after it was built - which is what a hostile or buggy producer would do.
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def gold_state(**overrides) -> ea.SymbolState:
    defaults = {"ask": 4000.05, "bid": 4000.03, "point": 0.01, "digits": 2, "stops_level": 1,
                "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01}
    defaults.update(overrides)
    return ea.SymbolState(**defaults)


# ------------------------------------------------------------------ declared rules
def test_the_ceilings_the_ea_refuses_above_are_the_ceilings_the_python_side_enforces():
    assert ea.ea_constant("EA_VOLUME_CEILING") == runner.MAX_DEMO_VOLUME
    assert ea.ea_constant("EA_SLIPPAGE_CEILING") == runner.MAX_SLIPPAGE_POINTS
    assert ea.ea_constant("EA_TTL_CEILING") == MAX_TTL_SECONDS
    assert ea.ea_constant("EA_POLL_CEILING") >= 1


def test_the_ea_defaults_cannot_exceed_the_ceilings_they_are_checked_against():
    assert ea.ea_input("MaxVolume") == ea.ea_constant("EA_VOLUME_CEILING")
    assert ea.ea_input("MaxSlippagePoints", "int") == ea.ea_constant("EA_SLIPPAGE_CEILING")
    assert ea.ea_input("MaxCommandTtlSeconds") == ea.ea_constant("EA_TTL_CEILING")
    assert 1 <= ea.ea_input("PollSeconds", "int") <= ea.ea_constant("EA_POLL_CEILING")
    assert int(ea.ea_input("MagicNumber", "long")) == runner.MAGIC_NUMBER


def test_the_demo_path_stays_a_source_change_rather_than_a_chart_input():
    # The compile-time constant is what makes "a preset cannot enable demo trading" true.
    assert re.search(r"^const bool ENABLE_DEMO_EXECUTION = false;", ea.EA_SOURCE_TEXT,
                     re.MULTILINE)


@pytest.mark.parametrize("source", [ea.ea_required_keys(), tuple(REQUIRED_KEYS)])
def test_the_json_schema_the_ea_and_the_bridge_all_demand_the_same_seventeen_fields(source):
    schema = json.loads((ROOT / "schemas" / "order_command.schema.json").read_text("utf-8"))
    assert set(source) == set(schema["required"])


# --------------------------------------------------------------------- the parser
def test_the_live_bridge_kill_switch_body_parses_and_reads_as_the_ea_reads_it(tmp_path):
    """The bytes on the socket, not a hand-written fixture: the bridge itself answers the poll."""
    store = KillSwitchStore(tmp_path / "kill_switch.json")
    store.record(active=True, actor_id="op", reason="ARMED", now=NOW)
    bridge = DemoBridge(token=TOKEN, tombstones_path=tmp_path / "tombstones.jsonl",
                        kill_switch_store=store, port=0)
    with bridge:
        armed_status, armed_body = get_switch_body(bridge.port)
        store.record(active=False, actor_id="op", reason="released for the parity test", now=NOW)
        released_status, released_body = get_switch_body(bridge.port)

    assert (armed_status, released_status) == (200, 200)
    # CheckKillSwitch() continues only on an exact false, so an armed switch must not read as one.
    assert ea.kill_switch_allows(armed_body) == (False, "BLOCKED: kill switch is active or "
                                                       "state is invalid.")
    assert ea.kill_switch_allows(released_body) == (True, "")


def test_a_missing_or_non_boolean_switch_field_blocks_rather_than_defaulting_to_open():
    for body in ('{"kill_switch_reason":"none"}', '{"kill_switch_active":"false"}',
                 '{"kill_switch_active":0}', '{"kill_switch_active":null}', "{}"):
        assert ea.kill_switch_allows(body)[0] is False, body


@pytest.mark.parametrize("body", [
    '{"a":1}{"b":2}',                    # two objects
    '{"nested":{"a":1}}',                # nesting
    '{"array":[1,2]}',                   # arrays
    '{"dup":1,"dup":2}',                 # duplicate key
    '{"escaped":"a\\"b"}',               # an escape sequence
    '{"ctl":"a\\u0001b"}',               # a control character written as an escape
    '{"a":1,}',                          # trailing comma
    '{"a":1',                            # unterminated
    'not json at all',
    '{"a":trueish}',                     # a boolean prefix that is not the literal
    '{"a":01}',                          # a leading zero
    '{"a":1e}',                          # an empty exponent
    '{"":"x"}',                          # an empty key
])
def test_bodies_python_json_accepts_that_the_ea_refuses_are_refused_by_the_port(body):
    assert ea.validate_flat_object(body)[0] is None


def test_the_port_refuses_what_the_bridge_sanitiser_strips(tmp_path):
    """The bridge scrubs characters the EA cannot parse; the scrub and the parser must agree.

    If the two rules drift, a reason string written by an operator would block every poll, and
    the operator would see a kill-switch error instead of the text they typed.
    """
    store = KillSwitchStore(tmp_path / "kill_switch.json")
    store.record(active=False, actor_id="op", reason='he said "stop" \\ and\nhid it', now=NOW)
    bridge = DemoBridge(token=TOKEN, tombstones_path=tmp_path / "tombstones.jsonl",
                        kill_switch_store=store, port=0)
    with bridge:
        status, body = get_switch_body(bridge.port)
    assert status == 200
    assert ea.kill_switch_allows(body) == (True, ""), body


# ------------------------------------------------------------------ value fidelity
def test_every_field_the_runner_writes_survives_the_ea_parser_with_its_value(tmp_path):
    body = command_body()
    parsed, error = ea.validate_flat_object(body)
    assert parsed is not None, error
    payload = json.loads(body)
    for key in REQUIRED_KEYS:
        wanted = payload[key]
        if isinstance(wanted, bool):
            assert parsed.json_bool_exact(key, wanted), key
        elif isinstance(wanted, (int, float)):
            assert parsed.json_number(key) == wanted, key
        else:
            assert parsed.json_string(key) == wanted, key


@pytest.mark.parametrize("token", ["0", "-0", "0.5", "4000.0", "1e-05", "1E+2", "-1.5e-3",
                                   "1796000060", "0.2", "-0.0", "1.7976931348623157e+308"])
def test_the_number_grammar_the_ea_accepts_is_the_grammar_json_accepts(token):
    assert ea.is_json_number_token(token) == _json_accepts_number(token)


@pytest.mark.parametrize("token", ["01", "+1", "1.", ".5", "-", "1e", "1e+", "0x10", "--1",
                                   "1.2.3", "", "nan", "Infinity"])
def test_a_malformed_number_is_refused_by_the_ea_port_by_the_same_rule_as_json(token):
    assert ea.is_json_number_token(token) == _json_accepts_number(token)


def test_a_number_that_json_would_write_as_infinity_is_not_queued_at_all():
    # allow_nan=False is what keeps the parser honest: no EA-side value can be inf or NaN.
    with pytest.raises(ValueError):
        json.dumps({"size": float("inf")}, allow_nan=False)


# ---------------------------------------------------------------- identifiers, tokens
@pytest.mark.parametrize("value", [
    "cmd-1", "ord_2", "XAUUSDm", "run:7", "a.b.c", "A" * 128, "", "A" * 129, "a b", "a/b",
    'a"b', "Золото", "a\nb", "a\x01b",
])
def test_the_ea_identifier_rule_and_the_bridge_regex_accept_the_same_names(value):
    assert ea.is_safe_identifier(value) == bool(IDENTIFIER_PATTERN.fullmatch(value))


@pytest.mark.parametrize("token", [
    "a" * 24, "a" * 256, "a" * 23, "a" * 257, "", "a" * 30 + " ", "a" * 30 + "+",
    "Abcdefghijklmnopqrstuvwxyz0123456789._~-", "a" * 30 + "\r",
])
def test_the_ea_token_rule_and_the_bridge_token_rule_are_the_same_rule(token):
    assert ea.is_valid_bridge_token(token) == bool(TOKEN_PATTERN.fullmatch(token))


def test_the_token_the_start_script_generates_is_one_the_ea_would_accept():
    """system_start.ps1 mints the bridge secret; the EA has to be able to send it back."""
    import string

    script = (ROOT / "scripts" / "system_start.ps1").read_text(encoding="utf-8")
    alphabet = re.search(r'\$alpha = "([^"]+)"', script).group(1)
    drawn = re.search(r"0\.\.(\d+) \| ForEach", script).group(1)
    legal = set(string.ascii_letters + string.digits) | set("._~-")
    # A character the header rule rejects would make every freshly generated token fail with a
    # 401 on the chart, and the operator would read that as a broken bridge.
    assert set(alphabet) <= legal
    assert int(drawn) + 1 == 44  # the token length the script mints
    assert ea.is_valid_bridge_token(alphabet[:44]) and len(alphabet[:44]) == 44


# --------------------------------------------------------------------------- urls
@pytest.mark.parametrize("url,accepted", [
    ("http://127.0.0.1:8000/v1/risk/kill-switch", True),
    ("http://localhost:8000/v1/execution/next", True),
    ("http://127.0.0.1:1/x", True),
    ("http://127.0.0.1:65535/x", True),
    ("http://127.0.0.1:8000@attacker.example/v1/execution/next", False),
    ("http://127.0.0.1:8000?", False),
    ("http://127.0.0.1:8000#", False),
    ("http://127.0.0.1:8000", True),          # no path: legal URL, the poll itself would 404
    ("http://127.0.0.1:8000/", False),
    ("http://127.0.0.1/x", False),             # no explicit port
    ("http://127.0.0.2:8000/x", False),
    ("http://localhost.localdomain:8000/x", False),
    ("http://127.0.0.1:08000/x", False),       # leading zero
    ("http://127.0.0.1:0/x", False),
    ("http://127.0.0.1:65536/x", False),
    ("https://127.0.0.1:8000/x", False),
    ("http://127.0.0.1:8000 /x", False),
    ("http://127.0.0.1:8000/\\x", False),
    ("http://127.0.0.1:8000/ключ", False),
])
def test_the_ea_loopback_rule_and_the_bridges_own_address_agree(url, accepted):
    assert ea.is_loopback_http_url(url)[0] is accepted


def test_the_urls_the_bridge_asks_the_operator_to_configure_pass_its_own_rule(tmp_path):
    """The EA inputs the runbook tells you to paste are the URLs the started bridge serves."""
    bridge = DemoBridge(token=TOKEN, tombstones_path=tmp_path / "unused.jsonl",
                        kill_switch_store=KillSwitchStore(tmp_path / "switch.json"), port=0)
    started = bridge.start()
    try:
        for path in (KILL_SWITCH_PATH, "/v1/execution/next"):
            url = f"{started.base_url}{path}"
            assert ea.is_loopback_http_url(url)[0] is True, url
    finally:
        started.stop()


# ------------------------------------------------------------------------ the gate
def test_a_clean_command_reaches_the_send_with_the_levels_the_broker_would_see():
    reason, request = ea.execute_demo_command(command_body(), now_epoch=NOW_EPOCH,
                                              state=gold_state())
    assert reason == ""
    assert request["price"] == 4000.05
    assert request["sl"] == 3990.0 and request["tp"] == 4020.0
    assert request["volume"] == 0.20
    assert request["deviation"] == int(ea.ea_input("MaxSlippagePoints", "int"))
    assert request["filling"] == "FOK"
    # Both ids are tombstoned before the send, and neither key is the raw id.
    assert len(request["tombstones"]) == 2
    assert all(not key.endswith(("cmd-1", "ord-1")) for key in request["tombstones"])


@pytest.mark.parametrize("mutate,reason", [
    (lambda: command_body(mode="live"),
     "BLOCKED: demo mode and explicit user approval are required."),
    (lambda: command_body(user_decision="DECLINE"),
     "BLOCKED: demo mode and explicit user approval are required."),
    (lambda: command_body(user_approved=False),
     "BLOCKED: demo mode and explicit user approval are required."),
    (lambda: command_body(direction="buy"), "BLOCKED: unsupported direction: buy",),
    (lambda: command_body(size=0.0), "BLOCKED: invalid volume/SL/TP/expected price."),
    (lambda: command_body(size=0.21), "BLOCKED: invalid volume/SL/TP/expected price."),
    (lambda: command_body(sl=0.0), "BLOCKED: invalid volume/SL/TP/expected price."),
    (lambda: command_body(ttl_seconds=int(ea.ea_constant("EA_TTL_CEILING")) + 1),
     "BLOCKED: command TTL exceeds MaxCommandTtlSeconds;"),
    (lambda: command_body(ttl_seconds=-5), "BLOCKED: command expired."),
    (lambda: command_body(instrument="XAU USD"),
     "BLOCKED: command/order/symbol identifier contains unsupported characters."),
    (lambda: command_body(now=NOW - timedelta(seconds=120), ttl_seconds=60),
     "BLOCKED: command expired."),
], ids=["mode", "decision", "approval", "direction", "zero lot", "cap lot", "zero stop",
        "ttl ceiling", "negative ttl", "space in symbol", "already expired"])
def test_each_broken_command_is_refused_for_the_reason_the_ea_prints(mutate, reason):
    captured, request = ea.execute_demo_command(mutate(), now_epoch=NOW_EPOCH, state=gold_state())
    assert captured.startswith(reason), captured
    assert request == {}


def test_an_instrument_that_cannot_be_traded_and_a_dead_account_block_before_any_price():
    closed = gold_state(trade_mode="disabled")
    assert ea.execute_demo_command(command_body(), now_epoch=NOW_EPOCH,
                                   state=closed)[0].startswith(
        "BLOCKED: symbol does not allow full trading")
    for kwargs in ({"demo_account": False}, {"terminal_trade_allowed": False},
                   {"account_trade_allowed": False}, {"demo_execution_enabled": False},
                   {"allow_live_execution": True}):
        reason, request = ea.execute_demo_command(command_body(), now_epoch=NOW_EPOCH,
                                                 state=gold_state(), **kwargs)
        assert reason and not request, kwargs


def test_a_command_already_attempted_is_blocked_by_either_tombstone():
    keys = frozenset({ea.command_tombstone_key("cmd-1"), ea.order_tombstone_key("ord-1")})
    for seen in (frozenset({ea.command_tombstone_key("cmd-1")}),
                 frozenset({ea.order_tombstone_key("ord-1")})):
        assert seen <= keys
        reason, request = ea.execute_demo_command(command_body(), now_epoch=NOW_EPOCH,
                                                 state=gold_state(), seen_tombstones=seen)
        assert reason == "DUPLICATE BLOCKED: command or order already attempted."
        assert request == {}
    # An unrelated command must not be blocked by somebody else's tombstone.
    assert ea.execute_demo_command(command_body(), now_epoch=NOW_EPOCH, state=gold_state(),
                                   seen_tombstones=frozenset({"CC_TRIED_C_other"}))[1] != {}


def test_the_guards_measure_the_rounded_levels_the_broker_receives():
    """A stop that clears the stops level unrounded must not round onto the price.

    digits=2, point=0.01, one point of stops level: 4000.0450 rounds to 4000.05 and 4000.0449
    rounds to 4000.04, so an unrounded distance of 0.0001 that is 0.01 points becomes a full
    point after rounding - and the reverse case is the one that used to slip through.
    """
    # Unrounded the stop is 0.0049 from the price (0.49 of a point), inside the one-point level;
    # rounded both land on the same tick, which is worse, and the EA must refuse either way.
    tight = command_body(expected_price=4000.0, sl=3999.9951, tp=4020.0)
    reason, request = ea.execute_demo_command(tight, now_epoch=NOW_EPOCH,
                                             state=gold_state(ask=4000.0049))
    assert not request
    assert reason in ("BLOCKED: SL/TP inside broker stops level (1 points).",
                      "BLOCKED: invalid BUY SL/TP geometry.")
    # The same numbers measured before rounding would have passed a 0.49-point distance check
    # only because 0.49 < 1 is a refusal, so the guard below is the one that has to fire.
    assert ea.normalize_double(4000.0049, 2) == 4000.00
    assert ea.normalize_double(3999.9951, 2) == 4000.00


def test_the_mirror_cannot_print_a_reason_the_ea_source_does_not_contain():
    """A reworded EA message is a documentation change, not a silent one."""
    printable = ea.ea_printable_messages()
    reasons = set()
    mutations = [command_body(), command_body(mode="live"), command_body(direction="hold"),
                 command_body(size=0.0), command_body(ttl_seconds=700), command_body(sl=0.0),
                 command_body(instrument="bad symbol"), '{"a":', '{"command_id":"c"}',
                 command_body(expected_price=1.0), command_body(now=NOW - timedelta(days=1),
                                                               ttl_seconds=60)]
    for body in mutations:
        reason, _ = ea.execute_demo_command(body, now_epoch=NOW_EPOCH, state=gold_state())
        if reason:
            reasons.add(reason)
    reason, _ = ea.execute_demo_command(command_body(), now_epoch=NOW_EPOCH,
                                       state=gold_state(trade_mode="disabled"))
    reasons.add(reason)
    for reason in reasons:
        head = reason.split(": ", 2)[0] + ": "
        assert any(fragment.startswith(head[:40]) for fragment in printable), reason


def test_the_runner_never_queues_a_lot_the_ea_would_refuse():
    """The lot step is checked with a tolerance on each side, and the looser one is the EA's.

    A millionth there against a billionth here means the runner can refuse a lot the chart would
    have taken, which costs a demo fill but never sends an order nobody approved. The reverse
    would be a BLOCKED line on the chart with nothing in the journal, so the subset is the
    invariant, not an equality.
    """
    spec = _Spec(point=0.00001, digits=5, stops_level_points=0, volume_min=0.01,
                 volume_max=100.0, volume_step=0.01)
    for micro in range(0, 400):
        volume = 0.01 + micro / 100000.0
        reasons = runner.volume_gates(volume, spec)
        state = gold_state(point=0.00001, digits=5, stops_level=0, ask=1.10000, bid=1.09998,
                           volume_step=0.01, volume_min=0.01, volume_max=100.0)
        body = command_body(size=volume, expected_price=1.1, sl=1.0990, tp=1.1010)
        _, request = ea.execute_demo_command(body, now_epoch=NOW_EPOCH, state=state,
                                             inputs=ea.EaInputs(max_volume=1.0))
        if not reasons:
            assert request, f"runner queued {volume}, the EA refused it"


def test_the_ea_and_the_runner_gate_the_same_levels_on_the_same_numbers():
    """``price_gates`` and the EA must not disagree about a broker stops level.

    The runner measures the numbers it holds; the broker receives the rounded ones. Where the two
    differ, the runner queues a command the chart then refuses, and the operator reads a BLOCKED
    line with no matching refusal in the journal. The grid walks the live price in tenths of a
    point, so the cases that straddle a rounding boundary are covered.
    """
    spec = _Spec(point=0.01, digits=2, stops_level_points=2, volume_min=0.01, volume_max=100.0,
                 volume_step=0.01)
    disagree = []
    for step in range(0, 200):
        live = 4000.0 + step / 1000.0
        # 1.02 points of stop distance against a two-point stops level: inside it unrounded, and
        # rounding price and stop to opposite ticks can put it exactly on the level.
        sl = live - 0.0102
        tp = live + 0.03
        proposal = {"direction": "BUY", "entry_price": live, "stop_loss": sl, "take_profit": tp}
        _, reasons = runner.price_gates(proposal, _Tick(ask=live, bid=live - 0.02), spec)
        state = gold_state(ask=live, point=0.01, digits=2, stops_level=2)
        ea_reason, request = ea.execute_demo_command(command_body(expected_price=live, sl=sl,
                                                                   tp=tp),
                                                     now_epoch=NOW_EPOCH, state=state)
        python_blocks = bool(reasons)
        if python_blocks != (not request):
            disagree.append((live, reasons, ea_reason))
    assert disagree == [], f"{len(disagree)} of 200 prices gated differently: {disagree[:3]}"


class _Spec:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class _Tick:
    def __init__(self, *, ask: float, bid: float) -> None:
        self.ask = ask
        self.bid = bid


def _json_accepts_number(token: str) -> bool:
    """What a JSON parser (not Python's lenient one) would read as a number.

    ``json`` accepts ``NaN`` and ``Infinity`` as extensions, and the bridge writes with
    ``allow_nan=False``; a strict ``parse_constant`` is what makes the comparison the EA's.
    """
    def _reject(constant: str) -> None:
        raise ValueError(f"not JSON: {constant}")

    try:
        loaded = json.loads(f'{{"v": {token}}}', parse_constant=_reject)
    except ValueError:
        return False
    return isinstance(loaded["v"], (int, float)) and not isinstance(loaded["v"], bool)
