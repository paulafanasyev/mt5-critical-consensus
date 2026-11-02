"""A Python transliteration of the MQL5 reference EA, checked against this repo's Python side.

``check_mql5_safety.py`` proves the EA source still contains the guards it must contain. That is a
static claim: it cannot notice that the EA and the runner compute the same rule differently. This
module is the runtime half. Every function below is a line-for-line port of a function in
``mql5/CriticalConsensusEA.mq5``, and ``tests/test_ea_logic_parity.py`` feeds both implementations
the same numbers - including the bodies the real bridge writes to the socket - and requires the
same answer.

Run directly to see the parity checks::

    python scripts/ea_logic_mirror.py
"""
from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EA_SOURCE_PATH = ROOT / "mql5" / "CriticalConsensusEA.mq5"
EA_SOURCE_TEXT = EA_SOURCE_PATH.read_text(encoding="utf-8")

TYPE_STRING = 1
TYPE_NUMBER = 2
TYPE_TRUE = 3
TYPE_FALSE = 4

_WS = " \t\r\n"


# --------------------------------------------------------------------- parsing
# Mirrors IsJsonDigits() / IsJsonNumberToken(). MQL5 indexes UTF-16 code units; a body carrying
# anything outside ASCII is covered by the refusal tests rather than assumed harmless.
def _is_digits(text: str, start: int, end: int) -> bool:
    if end <= start:
        return False
    for i in range(start, end):
        if not ("0" <= text[i] <= "9"):
            return False
    return True


def is_json_number_token(text: str) -> bool:
    n = len(text)
    if n == 0:
        return False
    i = 1 if text[0] == "-" else 0
    int_start = i
    while i < n and "0" <= text[i] <= "9":
        i += 1
    if not _is_digits(text, int_start, i):
        return False
    if i - int_start > 1 and text[int_start] == "0":
        return False
    if i < n and text[i] == ".":
        i += 1
        frac_start = i
        while i < n and "0" <= text[i] <= "9":
            i += 1
        if not _is_digits(text, frac_start, i):
            return False
    if i < n and text[i] in ("e", "E"):
        i += 1
        if i < n and text[i] in ("+", "-"):
            i += 1
        exp_start = i
        while i < n and "0" <= text[i] <= "9":
            i += 1
        if not _is_digits(text, exp_start, i):
            return False
    return i == n


class ParsedObject:
    """The g_validated_keys / values / types slots the EA fills while it parses."""

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.values: list[str] = []
        self.types: list[int] = []

    def store(self, key: str, value: str, value_type: int) -> None:
        self.keys.append(key)
        self.values.append(value)
        self.types.append(value_type)

    def index(self, key: str) -> int:
        for position, candidate in enumerate(self.keys):
            if candidate == key:
                return position
        return -1

    def key_in_list(self, key: str) -> bool:
        return self.index(key) >= 0

    def json_string(self, key: str) -> str:
        index = self.index(key)
        if index < 0 or self.types[index] != TYPE_STRING:
            return ""
        return self.values[index]

    def json_number(self, key: str) -> float:
        index = self.index(key)
        if index < 0 or self.types[index] != TYPE_NUMBER:
            return 0.0
        return float(self.values[index])

    def json_bool_exact(self, key: str, wanted: bool) -> bool:
        index = self.index(key)
        if index < 0:
            return False
        return self.types[index] == (TYPE_TRUE if wanted else TYPE_FALSE)

    def json_true(self, key: str) -> bool:
        return self.json_bool_exact(key, True)

    def json_false(self, key: str) -> bool:
        return self.json_bool_exact(key, False)


def _skip_ws(text: str, i: int, n: int) -> int:
    while i < n and text[i] in _WS:
        i += 1
    return i


def validate_flat_object(body: str) -> tuple[ParsedObject | None, str]:
    """Mirrors ValidateFlatObject(): one flat object of scalars, or nothing is recorded."""
    parsed = ParsedObject()
    text = body.strip()
    n = len(text)
    if n < 2 or text[0] != "{" or text[n - 1] != "}":
        return None, "body must be exactly one JSON object"
    i = 1
    after_comma = False
    while True:
        i = _skip_ws(text, i, n)
        if i >= n:
            return None, "unterminated object"
        c = text[i]
        if c == "}":
            if i != n - 1:
                return None, "trailing content after object"
            if after_comma:
                return None, "trailing comma"
            return parsed, ""
        if c in "{[":
            return None, "nested object or array rejected"
        if c != '"':
            return None, "object key must be a string"

        i += 1
        key_start = i
        while i < n and text[i] != '"':
            if text[i] == "\\":
                return None, "escape sequences rejected"
            if ord(text[i]) < 0x20:
                return None, "control character rejected"
            i += 1
        if i >= n:
            return None, "unterminated object key"
        key = text[key_start:i]
        if not key:
            return None, "empty object key"
        if parsed.key_in_list(key):
            return None, "DUPLICATE JSON KEY: " + key
        i += 1

        i = _skip_ws(text, i, n)
        if i >= n or text[i] != ":":
            return None, "missing ':' after key"
        i += 1
        i = _skip_ws(text, i, n)
        if i >= n:
            return None, "missing value"
        c = text[i]
        if c in "{[":
            return None, "nested value rejected"
        if c == '"':
            i += 1
            value_start = i
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    return None, "escape sequences rejected"
                if ord(text[i]) < 0x20:
                    return None, "control character rejected"
                i += 1
            if i >= n:
                return None, "unterminated string value"
            parsed.store(key, text[value_start:i], TYPE_STRING)
            i += 1
        elif c in "tf":
            token = "true" if c == "t" else "false"
            length = len(token)
            if i + length >= n or text[i:i + length] != token:
                return None, "invalid boolean literal"
            parsed.store(key, token, TYPE_TRUE if c == "t" else TYPE_FALSE)
            i += length
        elif ("0" <= c <= "9") or c == "-":
            number_start = i
            while i < n and text[i] not in ",}" and text[i] not in _WS:
                i += 1
            number_token = text[number_start:i]
            if not is_json_number_token(number_token):
                return None, "invalid number literal"
            value = float(number_token)
            if not math.isfinite(value):
                return None, "invalid number literal"
            parsed.store(key, number_token, TYPE_NUMBER)
        else:
            return None, "unsupported value type"

        i = _skip_ws(text, i, n)
        if i >= n:
            return None, "unterminated object"
        c = text[i]
        if c == ",":
            i += 1
            after_comma = True
            continue
        if c == "}":
            if i != n - 1:
                return None, "trailing content after object"
            return parsed, ""
        return None, "unexpected character after value"


# ------------------------------------------------------------------ identifiers
def is_safe_identifier(value: str) -> bool:
    """Mirrors IsSafeIdentifier()."""
    if len(value) < 1 or len(value) > 128:
        return False
    return all(("0" <= c <= "9") or ("A" <= c <= "Z") or ("a" <= c <= "z")
               or c in "-_.:" for c in value)


def is_valid_bridge_token(token: str) -> bool:
    """Mirrors IsValidBridgeToken()."""
    if len(token) < 24 or len(token) > 256:
        return False
    return all(("0" <= c <= "9") or ("A" <= c <= "Z") or ("a" <= c <= "z")
               or c in "._~-" for c in token)


def fnv1a_hex(text: str) -> str:
    """Mirrors Fnv1aHex(): FNV-1a over the low then high byte of each UTF-16 code unit."""
    hash_value = ((0xCBF29CE4 << 32) | 0x84222325) & 0xFFFFFFFFFFFFFFFF
    for char in text:
        code = ord(char)
        hash_value ^= code & 0xFF
        hash_value = (hash_value * 1099511628211) & 0xFFFFFFFFFFFFFFFF
        hash_value ^= (code >> 8) & 0xFF
        hash_value = (hash_value * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{hash_value:016X}"


def command_tombstone_key(command_id: str) -> str:
    return "CC_TRIED_C_" + fnv1a_hex(command_id)


def order_tombstone_key(order_id: str) -> str:
    return "CC_TRIED_O_" + fnv1a_hex(order_id)


# ------------------------------------------------------------------------ urls
def is_loopback_http_url(url: str) -> tuple[bool, str]:
    """Mirrors IsLoopbackHttpUrl()."""
    n = len(url)
    if n < 17:
        return False, "URL is too short"
    if url[:7] != "http://":
        return False, "URL must start with http://"
    for c in url:
        if ord(c) < 0x21 or ord(c) > 0x7E:
            return False, "URL contains whitespace, control or non-ASCII"
        if c in "@#?\\":
            return False, "URL contains a forbidden character"
    colon = url.find(":", 7)
    if colon < 0:
        return False, "URL must include an explicit port"
    host = url[7:colon]
    if host not in ("127.0.0.1", "localhost"):
        return False, "URL host must be exactly 127.0.0.1 or localhost"
    path = url.find("/", colon + 1)
    port_text = url[colon + 1:] if path < 0 else url[colon + 1:path]
    length = len(port_text)
    if length < 1 or length > 5:
        return False, "URL port must have 1..5 digits"
    if port_text[0] == "0":
        return False, "URL port must have no leading zero"
    port = 0
    for c in port_text:
        if not ("0" <= c <= "9"):
            return False, "URL port must be numeric"
        port = port * 10 + (ord(c) - ord("0"))
    if port < 1 or port > 65535:
        return False, "URL port is out of range"
    if path >= 0 and path == n - 1:
        return False, "URL path is empty after '/'"
    return True, ""


# ------------------------------------------------------------------ arithmetic
def normalize_double(value: float, digits: int) -> float:
    """Mirrors NormalizeDouble(), which is MathRound to the digit: halves away from zero."""
    factor = 10.0 ** digits
    result = math.floor(abs(value) * factor + 0.5) / factor
    return -result if value < 0 else result


def _source_number(pattern: str, label: str) -> float:
    match = re.search(pattern, EA_SOURCE_TEXT, re.MULTILINE)
    if not match:
        raise AssertionError(f"{label} not found in {EA_SOURCE_PATH}")
    return float(match.group(1).strip())


def ea_input(name: str, kind: str = "double") -> float:
    return _source_number(rf"^input\s+{kind}\s+{name}\s*=\s*([^;]+);", f"EA input {name}")


def ea_constant(name: str) -> float:
    return _source_number(rf"^const\s+(?:double|int|long)\s+{name}\s*=\s*([^;]+);",
                          f"EA constant {name}")


def ea_required_keys() -> tuple[str, ...]:
    """The required_keys[] literal in ExecuteDemoCommand(), in the order the EA checks it."""
    match = re.search(r"string required_keys\[\]\s*=\s*\{(.*?)\}", EA_SOURCE_TEXT, re.DOTALL)
    if not match:
        raise AssertionError("required_keys[] literal not found in the EA source")
    return tuple(re.findall(r'"([^"]+)"', match.group(1)))


def ea_printable_messages() -> tuple[str, ...]:
    """Every string literal the EA can print, so a mirror reason not in the source fails."""
    return tuple(re.findall(r'"((?:[^"\\]|\\.)*)"', EA_SOURCE_TEXT))


# ------------------------------------------------------------- command execution
class SymbolState:
    """The per-call market facts the EA reads from SymbolInfo*()."""

    def __init__(self, *, ask: float, bid: float, point: float, digits: int,
                 stops_level: int, volume_min: float, volume_max: float,
                 volume_step: float, trade_mode: str = "full",
                 filling_fok: bool = True, filling_ioc: bool = False) -> None:
        self.ask = ask
        self.bid = bid
        self.point = point
        self.digits = digits
        self.stops_level = stops_level
        self.volume_min = volume_min
        self.volume_max = volume_max
        self.volume_step = volume_step
        self.trade_mode = trade_mode
        self.filling_fok = filling_fok
        self.filling_ioc = filling_ioc


class EaInputs:
    """The chart inputs ExecuteDemoCommand measures against, defaulted from the source."""

    def __init__(self, *, max_volume: float | None = None,
                 max_slippage_points: int | None = None,
                 max_ttl_seconds: float | None = None) -> None:
        self.max_volume = ea_input("MaxVolume") if max_volume is None else max_volume
        self.max_slippage_points = (int(ea_input("MaxSlippagePoints", "int"))
                                    if max_slippage_points is None else max_slippage_points)
        self.max_ttl_seconds = (ea_input("MaxCommandTtlSeconds")
                                if max_ttl_seconds is None else max_ttl_seconds)


def execute_demo_command(body: str, *, now_epoch: float, state: SymbolState,
                         inputs: EaInputs | None = None,
                         demo_account: bool = True,
                         terminal_trade_allowed: bool = True,
                         account_trade_allowed: bool = True,
                         demo_execution_enabled: bool = True,
                         allow_live_execution: bool = False,
                         seen_tombstones: frozenset[str] = frozenset(),
                         ) -> tuple[str, dict[str, object]]:
    """Return ``("", request)`` where the EA would send, or ``(reason, {})`` where it blocks.

    The reasons are the EA's own literals; the parity test asserts each one the mirror can return
    is printable by the EA, so rewording a message in the source fails a test.
    """
    inputs = inputs or EaInputs()
    parsed, parse_error = validate_flat_object(body)
    if parsed is None:
        return f"BLOCKED: command body rejected: {parse_error}", {}

    for key in ea_required_keys():
        if not parsed.key_in_list(key):
            return f"BLOCKED: required OrderCommand field missing: {key}", {}

    command_id = parsed.json_string("command_id")
    order_id = parsed.json_string("client_order_id")
    symbol = parsed.json_string("instrument")
    direction = parsed.json_string("direction")
    mode = parsed.json_string("mode")
    decision = parsed.json_string("user_decision")
    volume = parsed.json_number("size")
    sl = parsed.json_number("sl")
    tp = parsed.json_number("tp")
    expected_price = parsed.json_number("expected_price")
    expires_raw = parsed.json_number("expires_at_epoch")
    user_approved = parsed.json_true("user_approved")

    if not command_id or not order_id or not symbol or not direction or not mode or not decision:
        return "BLOCKED: required OrderCommand field missing.", {}
    if not (is_safe_identifier(command_id) and is_safe_identifier(order_id)
            and is_safe_identifier(symbol)):
        return ("BLOCKED: command/order/symbol identifier contains unsupported characters.", {})
    if direction not in ("BUY", "SELL"):
        return f"BLOCKED: unsupported direction: {direction}", {}
    if mode != "demo" or decision != "APPROVE" or not user_approved:
        return "BLOCKED: demo mode and explicit user approval are required.", {}
    if not demo_account:
        return "BLOCKED: account is not demo. This EA does not execute live trades.", {}
    if not terminal_trade_allowed or not account_trade_allowed:
        return "BLOCKED: terminal or account trading is not allowed.", {}
    if not demo_execution_enabled or allow_live_execution:
        return "BLOCKED: demo execution is disabled, or live mode was requested.", {}
    if expires_raw <= 0 or math.floor(expires_raw) != expires_raw:
        return "BLOCKED: expires_at_epoch is not a positive whole number.", {}
    if expires_raw <= now_epoch:
        return "BLOCKED: command expired.", {}
    if expires_raw > now_epoch + inputs.max_ttl_seconds:
        return (f"BLOCKED: command TTL exceeds MaxCommandTtlSeconds; expires_at={expires_raw}",
                {})
    if volume <= 0 or volume > inputs.max_volume or sl <= 0 or tp <= 0 or expected_price <= 0:
        return "BLOCKED: invalid volume/SL/TP/expected price.", {}

    command_key = command_tombstone_key(command_id)
    order_key = order_tombstone_key(order_id)
    if command_key in seen_tombstones or order_key in seen_tombstones:
        return "DUPLICATE BLOCKED: command or order already attempted.", {}

    if state.trade_mode != "full":
        return f"BLOCKED: symbol does not allow full trading (mode={state.trade_mode}).", {}
    if state.point <= 0:
        return "BLOCKED: invalid symbol point.", {}

    live_price = state.ask if direction == "BUY" else state.bid
    order_price = normalize_double(live_price, state.digits)
    order_sl = normalize_double(sl, state.digits)
    order_tp = normalize_double(tp, state.digits)
    if order_price <= 0 or order_sl <= 0 or order_tp <= 0:
        return ("BLOCKED: rounding price/SL/TP to symbol digits produced a non-positive level.",
                {})
    if abs(order_price - expected_price) > inputs.max_slippage_points * state.point:
        return "BLOCKED: slippage guard triggered.", {}
    if direction == "BUY" and not order_sl < order_price < order_tp:
        return "BLOCKED: invalid BUY SL/TP geometry.", {}
    if direction == "SELL" and not order_tp < order_price < order_sl:
        return "BLOCKED: invalid SELL SL/TP geometry.", {}

    stops = state.stops_level
    if abs(order_price - order_sl) < stops * state.point \
            or abs(order_tp - order_price) < stops * state.point:
        return f"BLOCKED: SL/TP inside broker stops level ({stops} points).", {}

    step = state.volume_step
    if step <= 0 or volume < state.volume_min or volume > state.volume_max \
            or abs(volume / step - round(volume / step)) > 0.000001:
        return "BLOCKED: broker volume constraints not satisfied.", {}

    if not state.filling_fok and not state.filling_ioc:
        return "BLOCKED: symbol advertises neither FOK nor IOC for a market deal.", {}

    return "", {"symbol": symbol, "volume": volume, "price": order_price, "sl": order_sl,
                "tp": order_tp, "deviation": inputs.max_slippage_points,
                "filling": "FOK" if state.filling_fok else "IOC",
                "tombstones": sorted({command_key, order_key})}


def kill_switch_allows(body: str) -> tuple[bool, str]:
    """Mirrors CheckKillSwitch() once the HTTP status is known: only an exact false lets a poll
    continue, so an absent field, a string, a duplicate or a malformed body all block."""
    parsed, error = validate_flat_object(body)
    if parsed is None:
        return False, f"BLOCKED: kill-switch response is not a flat JSON object: {error}"
    if not parsed.key_in_list("kill_switch_active"):
        return False, "BLOCKED: kill-switch response has no kill_switch_active field."
    if not parsed.json_false("kill_switch_active"):
        return False, "BLOCKED: kill switch is active or state is invalid."
    return True, ""


# ------------------------------------------------------------------- self-check
def _check(number: int, label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        raise AssertionError(f"CHECK {number} FAILED - {label}: {detail}")
    print(f"CHECK {number} OK  - {label}" + (f" ({detail})" if detail else ""))


def run() -> int:
    """A short self-test of the mirror; the cross-implementation assertions are in pytest."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import mt5_demo_bridge
    import run_mt5_demo_trading as runner

    released = json.dumps({"kill_switch_active": False, "kill_switch_reason": "RELEASED"},
                          ensure_ascii=False, separators=(",", ":"))
    armed = json.dumps({"kill_switch_active": True, "kill_switch_reason": "ARMED"},
                       ensure_ascii=False, separators=(",", ":"))
    _check(1, "the bridge's own kill-switch bodies parse, and only the released one lets a poll "
              "continue", kill_switch_allows(released)[0] and not kill_switch_allows(armed)[0])

    now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    command = runner.build_command(command_id="c" * 32, client_order_id="order-1",
                                   permission_id="p-1", signal_id="s-1", instrument="XAUUSD",
                                   direction="BUY", size=0.20, expected_price=4000.0,
                                   sl=3990.0, tp=4020.0, user_decision_id="d-1", now=now,
                                   ttl_seconds=60)
    body = json.dumps(command, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    parsed, error = validate_flat_object(body)
    _check(2, "every OrderCommand field the runner writes survives the EA's parser",
           parsed is not None and parsed.json_string("instrument") == "XAUUSD"
           and parsed.json_number("size") == 0.20 and parsed.json_true("user_approved")
           and parsed.json_string("mode") == "demo", error)

    state = SymbolState(ask=4000.05, bid=4000.03, point=0.01, digits=2, stops_level=1,
                        volume_min=0.01, volume_max=100.0, volume_step=0.01)
    reason, request = execute_demo_command(body, now_epoch=now.timestamp(), state=state)
    _check(3, "a command the runner builds reaches the send with rounded levels",
           request.get("price") == 4000.05 and request.get("filling") == "FOK", reason)

    _check(4, "the ceilings the EA refuses above are the ceilings the runner enforces",
           ea_constant("EA_VOLUME_CEILING") == runner.MAX_DEMO_VOLUME
           and ea_constant("EA_SLIPPAGE_CEILING") == runner.MAX_SLIPPAGE_POINTS
           and ea_constant("EA_TTL_CEILING") == mt5_demo_bridge.MAX_TTL_SECONDS)
    _check(5, "the required-key list in the EA is the list the bridge validates",
           set(ea_required_keys()) == set(mt5_demo_bridge.REQUIRED_KEYS),
           f"{len(mt5_demo_bridge.REQUIRED_KEYS)} fields")
    _check(6, "the tombstone hash is FNV-1a over the UTF-16LE bytes the EA walks, and distinct "
              "ids stay distinct",
           fnv1a_hex("") == "CBF29CE484222325"
           and fnv1a_hex("a") == "089BE207B544F1E4"
           and fnv1a_hex("foobar") == "C882D5010E965F1A"
           and command_tombstone_key("order-1") != command_tombstone_key("order-2"))
    _check(7, "the loopback rule takes the bridge's URLs and rejects a userinfo lookalike",
           is_loopback_http_url(f"http://127.0.0.1:8000{mt5_demo_bridge.KILL_SWITCH_PATH}")[0]
           and not is_loopback_http_url("http://127.0.0.1:8000@attacker.example/x")[0])
    _check(8, "the number grammar the EA accepts is the grammar JSON allows",
           all(is_json_number_token(token) == _json_number_ok(token)
               for token in ("0", "-0", "0.5", "1e-05", "1E+2", "01", "1.", ".5", "+1", "1e")))
    print("\nALL 8 CHECKS PASSED - the EA's rules and this Python mirror agree.")
    return 0


def _json_number_ok(token: str) -> bool:
    """Strict JSON, not Python's: NaN and Infinity are extensions the bridge never writes."""
    def _reject(constant: str) -> None:
        raise ValueError(constant)

    try:
        loaded = json.loads(f'{{"v": {token}}}', parse_constant=_reject)
    except ValueError:
        return False
    return isinstance(loaded["v"], (int, float)) and not isinstance(loaded["v"], bool)


if __name__ == "__main__":
    sys.exit(run())
