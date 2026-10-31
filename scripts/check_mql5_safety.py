#!/usr/bin/env python3
"""Static safety guard checks for the EA source; this is not a MetaEditor compile test.

The checker is importable (``check_source``/``self_test``) so tests can prove that
weakening the EA is detected. Every invariant is a text-level guard: it catches
accidental regressions, not a determined rewrite, and it cannot prove compilation,
runtime behaviour, or broker-side execution.

Comments are stripped before matching, so a commented-out guard can never satisfy a
structural check. Predicates are pinned to their exact comparison direction, operand
order and right-hand delimiter, because a flipped ``==``/``!=`` or a threshold
multiplied past the closing parenthesis is the most common silent weakening. Each
``Print("BLOCKED...")`` branch must also contain its own ``return false;``: keeping the
message but dropping the return would otherwise leave a guard that only logs. Guards
whose message must survive are pinned as complete ``if (<cond>) { <body> }`` statements
compared with whitespace removed, so a branch cannot be neutralised while its text stays
in place, and a parenthesised boolean literal used as a term (``&& (false)``) is rejected
outright because it disables the surrounding expression without deleting any predicate.
Because a matching statement could also be an unreachable copy, each pinned guard must
appear exactly once inside its function and ahead of the send or the last success return;
values the guards validate (stops, URLs, the expiry clock, the approval flag) are likewise
pinned where they are consumed, not only where they are checked.

A Python reference model of the EA's flat-object parser is exercised against the shared
fixtures in ``config/mql5_flat_object_cases.json``. The fixtures are bound to the EA in
both directions: every fixture reason must appear in the EA parser and every error
literal in the EA parser must have a fixture that provokes it.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EA = ROOT / "mql5" / "CriticalConsensusEA.mq5"
FLAT_OBJECT_CASES = ROOT / "config" / "mql5_flat_object_cases.json"


def _strip_comments(source: str) -> str:
    """Remove // and /* */ comments while preserving string and char literals."""
    out: list[str] = []
    i = 0
    n = len(source)
    state = "code"
    while i < n:
        ch = source[i]
        if state == "code":
            if ch == "/" and i + 1 < n and source[i + 1] == "/":
                state = "line"
                i += 2
                continue
            if ch == "/" and i + 1 < n and source[i + 1] == "*":
                state = "block"
                i += 2
                continue
            if ch == '"':
                state = "string"
                out.append(ch)
                i += 1
                continue
            if ch == "'":
                state = "char"
                out.append(ch)
                i += 1
                continue
            out.append(ch)
            i += 1
        elif state == "string":
            if ch == "\\" and i + 1 < n:
                out.append(ch)
                out.append(source[i + 1])
                i += 2
                continue
            out.append(ch)
            if ch == '"' or ch == "\n":
                state = "code"
            i += 1
        elif state == "char":
            if ch == "\\" and i + 1 < n:
                out.append(ch)
                out.append(source[i + 1])
                i += 2
                continue
            out.append(ch)
            if ch == "'" or ch == "\n":
                state = "code"
            i += 1
        elif state == "line":
            if ch == "\n":
                out.append(ch)
                state = "code"
            i += 1
        else:  # block comment
            if ch == "*" and i + 1 < n and source[i + 1] == "/":
                state = "code"
                i += 2
                continue
            if ch == "\n":
                out.append(ch)
            i += 1
    return "".join(out)


def _scan_to(source: str, start: int, target: str) -> int:
    """Index of ``target`` at or after ``start``, skipping string and char literals."""
    i = start
    n = len(source)
    while i < n:
        ch = source[i]
        if ch in "\"'":
            quote = ch
            i += 1
            while i < n:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == target:
            return i
        i += 1
    return -1


def _required_checks() -> dict[str, str]:
    return {
        # Fail-closed switches and the demo-only account gate. Demo execution is a compile-time
        # constant, so no chart input, preset or template can enable it.
        "demo execution is compile-time disabled": r"const\s+bool\s+ENABLE_DEMO_EXECUTION\s*=\s*false\s*;",
        "live execution switch disabled by default": r"input\s+bool\s+AllowLiveExecution\s*=\s*false\s*;",
        "demo account comparison direction": (
            r"AccountInfoInteger\s*\(\s*ACCOUNT_TRADE_MODE\s*\)\s*!=\s*ACCOUNT_TRADE_MODE_DEMO\s*\)"
        ),
        "terminal and account trading allowed check": (
            r"if\s*\(\s*!TerminalInfoInteger\s*\(\s*TERMINAL_TRADE_ALLOWED\s*\)\s*\|\|\s*"
            r"!AccountInfoInteger\s*\(\s*ACCOUNT_TRADE_ALLOWED\s*\)\s*\)"
        ),
        "demo/live execution gate": r"if\s*\(\s*!ENABLE_DEMO_EXECUTION\s*\|\|\s*AllowLiveExecution\s*\)",
        "approval gate includes user approval": (
            r"if\s*\(\s*mode\s*!=\s*\"demo\"\s*\|\|\s*decision\s*!=\s*\"APPROVE\"\s*\|\|\s*!\s*user_approved\s*\)"
        ),
        "explicit user approval required": (
            r"bool\s+user_approved\s*=\s*JsonTrue\s*\(\s*\"user_approved\"\s*\)\s*;"
        ),
        # Command expiry, TTL and size limits. Right-anchored so a widened limit cannot match.
        "command expiry validation": r"if\s*\(\s*expires_raw\s*<=\s*now_epoch\s*\)",
        "expiry must be a whole positive number": (
            r"if\s*\(\s*expires_raw\s*<=\s*0\s*\|\|\s*MathFloor\s*\(\s*expires_raw\s*\)\s*!=\s*expires_raw\s*\)"
        ),
        "command TTL upper bound comparison": (
            r"if\s*\(\s*expires_raw\s*>\s*now_epoch\s*\+\s*MaxCommandTtlSeconds\s*\)"
        ),
        # The whole expiry/TTL comparison chain is relative to this one clock read. A shifted
        # origin (TimeGMT() - 315360000) silently resurrects year-old commands.
        "expiry clock origin is the terminal GMT time": (
            r"double\s+now_epoch\s*=\s*\(\s*double\s*\)\s*TimeGMT\s*\(\s*\)\s*;"
        ),
        "command TTL default": r"input\s+double\s+MaxCommandTtlSeconds\s*=\s*600\s*;",
        "volume cap comparison": r"volume\s*>\s*MaxVolume\s*\|\|",
        "volume cap is positive": r"volume\s*<=\s*0\s*\|\|\s*volume\s*>\s*MaxVolume",
        "volume cap default": r"input\s+double\s+MaxVolume\s*=\s*0\.20\s*;",
        "protective stop-loss and take-profit validation": (
            r"sl\s*<=\s*0\s*\|\|\s*tp\s*<=\s*0\s*\|\|\s*expected_price\s*<=\s*0\s*\)"
        ),
        # Validation without binding is useless: the checked locals must reach the order
        # request itself. Pinned to the exact NormalizeDouble(<local>, digits) form so a
        # detached or zeroed stop cannot satisfy the invariant.
        "protective stop-loss is bound to the order request": (
            r"request\.sl\s*=\s*NormalizeDouble\s*\(\s*sl\s*,\s*digits\s*\)\s*;"
        ),
        "protective take-profit is bound to the order request": (
            r"request\.tp\s*=\s*NormalizeDouble\s*\(\s*tp\s*,\s*digits\s*\)\s*;"
        ),
        # Volume and direction are validated as locals too; the request must consume those
        # exact values, never a second read or a fixed side.
        "validated volume is bound to the order request": r"request\.volume\s*=\s*volume\s*;",
        "validated direction selects the order type": (
            r"request\.type\s*=\s*\(\s*direction\s*==\s*\"BUY\"\s*\)\s*\?\s*"
            r"ORDER_TYPE_BUY\s*:\s*ORDER_TYPE_SELL\s*;"
        ),
        # Slippage is measured against the symbol point, never a raw price.
        "slippage guard uses point value": (
            r"MathAbs\s*\(\s*live_price\s*-\s*expected_price\s*\)\s*>\s*MaxSlippagePoints\s*\*\s*point\s*\)"
        ),
        "slippage default": r"input\s+int\s+MaxSlippagePoints\s*=\s*20\s*;",
        "broker request deviation": r"request\.deviation\s*=\s*MaxSlippagePoints\s*;",
        "duplicate order guard": r"if\s*\(\s*GlobalVariableCheck\s*\(\s*command_key\s*\)\s*\|\|\s*GlobalVariableCheck\s*\(\s*order_key\s*\)\s*\)",
        "duplicate order rejection message": r"DUPLICATE BLOCKED",
        # Symbol and broker preflight.
        "symbol requires full trading mode": r"trade_mode\s*!=\s*SYMBOL_TRADE_MODE_FULL",
        "BUY geometry check": (
            r"if\s*\(\s*direction\s*==\s*\"BUY\"\s*&&\s*!\s*\(\s*sl\s*<\s*live_price\s*&&\s*live_price\s*<\s*tp\s*\)\s*\)"
        ),
        "SELL geometry check": (
            r"if\s*\(\s*direction\s*==\s*\"SELL\"\s*&&\s*!\s*\(\s*tp\s*<\s*live_price\s*&&\s*live_price\s*<\s*sl\s*\)\s*\)"
        ),
        "broker stops level check": (
            r"MathAbs\s*\(\s*live_price\s*-\s*sl\s*\)\s*<\s*stops\s*\*\s*point\s*\|\|\s*"
            r"MathAbs\s*\(\s*tp\s*-\s*live_price\s*\)\s*<\s*stops\s*\*\s*point\s*\)"
        ),
        "broker stops level source": r"SYMBOL_TRADE_STOPS_LEVEL",
        "volume step constraint": (
            r"if\s*\(\s*step\s*<=\s*0\s*\|\|\s*volume\s*<\s*minvol\s*\|\|\s*volume\s*>\s*maxvol\s*\|\|\s*MathAbs"
        ),
        "volume step rounding": (
            r"MathAbs\s*\(\s*volume\s*/\s*step\s*-\s*MathRound\s*\(\s*volume\s*/\s*step\s*\)\s*\)\s*>\s*0\.000001\s*\)"
        ),
        # Filling mode must be advertised by the broker; no unadvertised fallback.
        "FOK advertisement check": (
            r"if\s*\(\s*\(\s*filling\s*&\s*\(\s*long\s*\)\s*SYMBOL_FILLING_FOK\s*\)\s*!=\s*0\s*\)\s*"
            r"request\.type_filling\s*=\s*ORDER_FILLING_FOK\s*;"
        ),
        "IOC advertisement check": (
            r"else\s+if\s*\(\s*\(\s*filling\s*&\s*\(\s*long\s*\)\s*SYMBOL_FILLING_IOC\s*\)\s*!=\s*0\s*\)\s*"
            r"request\.type_filling\s*=\s*ORDER_FILLING_IOC\s*;"
        ),
        "neither FOK nor IOC blocks the order": (
            r"else\s*\{\s*Print\(\s*\"BLOCKED: symbol advertises neither FOK nor IOC[^\"]*\"\);\s*return\s+false;\s*\}"
        ),
        "explicit order filling mode": r"request\.type_filling",
        # Broker preflight must be acted upon, not merely invoked.
        "broker preflight failure branch": (
            r"if\s*\(\s*!\s*OrderCheck\s*\(\s*request\s*,\s*check\s*\)\s*\)\s*\{\s*"
            r"Print\(\s*\"BLOCKED: OrderCheck failed[^\"]*\"[^}]*return\s+false;\s*\}"
        ),
        "post-preflight kill-switch re-read": (
            r"if\s*\(\s*!CheckKillSwitch\s*\(\s*\)\s*\)\s*\{\s*"
            r"Print\(\s*\"BLOCKED: kill switch changed before send\.\"\s*\);\s*return\s+false;\s*\}"
        ),
        "order result retcode validation": (
            r"result\.retcode\s*!=\s*TRADE_RETCODE_DONE\s*&&\s*result\.retcode\s*!=\s*TRADE_RETCODE_DONE_PARTIAL\s*&&\s*"
            r"result\.retcode\s*!=\s*TRADE_RETCODE_PLACED\s*\)"
        ),
        "order time in force": r"request\.type_time\s*=\s*ORDER_TIME_GTC\s*;",
        # Idempotency tombstones.
        "idempotency persist failure is fail-closed": r"GlobalVariableSet\s*\(\s*command_key\s*,\s*\(double\)TimeGMT\s*\(\s*\)\s*\)\s*==\s*0",
        "order tombstone persist failure is fail-closed": r"GlobalVariableSet\s*\(\s*order_key\s*,\s*\(double\)TimeGMT\s*\(\s*\)\s*\)\s*==\s*0",
        # MQL5 build 6295 declares GlobalVariablesFlush() as void, so the flush cannot gate the
        # send. Both writes must instead be observable before the send; durable idempotency is
        # the bridge's exact-ID record, not this in-memory tombstone.
        "tombstones are re-checked after persist": (
            r"if\s*\(\s*!\s*GlobalVariableCheck\s*\(\s*command_key\s*\)\s*\|\|\s*"
            r"!\s*GlobalVariableCheck\s*\(\s*order_key\s*\)\s*\)\s*\{\s*"
            r'Print\(\s*"BLOCKED:[^"]*"\s*\)\s*;\s*return\s+false\s*;\s*\}'
        ),
        "command tombstone hashes command_id": r'return\s+"CC_TRIED_C_"\s*\+\s*Fnv1aHex\s*\(\s*command_id\s*\)',
        "order tombstone hashes client_order_id": r'return\s+"CC_TRIED_O_"\s*\+\s*Fnv1aHex\s*\(\s*order_id\s*\)',
        "full-string FNV-1a hash": r"string\s+Fnv1aHex\s*\(",
        "FNV hashes both bytes of each character": r"hash\s*\^=\s*\(ulong\)\s*\(\s*\(\s*c\s*>>\s*8\s*\)\s*&\s*0xFF\s*\)",
        "FNV low byte mixing": r"hash\s*\^=\s*\(ulong\)\(\s*c\s*&\s*0xFF\s*\)",
        "FNV multiply constant": r"hash\s*\*=\s*1099511628211\s*;",
        "FNV offset basis halves": (
            r"hash\s*=\s*\(\(ulong\)0xCBF29CE4\s*<<\s*32\)\s*\|\s*\(ulong\)0x84222325\s*;"
        ),
        "FNV rendered as 16 uppercase hex digits": r'StringFormat\("%016I64X",\s*\(long\)hash\)',
        # Bridge transport.
        "localhost bridge default": r"http://127\.0\.0\.1:8000/v1/execution/next",
        "kill-switch endpoint configured": r"KillSwitchUrl",
        # Only the two URLs OnInit validates may be sent to. An extra input (or local) that
        # bypasses IsLoopbackHttpUrl would otherwise reach an arbitrary host.
        "command web request targets BridgeUrl": (
            r'WebRequest\s*\(\s*"GET"\s*,\s*BridgeUrl\s*,'
        ),
        "kill-switch web request targets KillSwitchUrl": (
            r'WebRequest\s*\(\s*"GET"\s*,\s*KillSwitchUrl\s*,'
        ),
        "kill-switch HTTP 200 required": r"if\s*\(\s*code\s*!=\s*200\s*\)",
        "kill-switch field must be present": r'KeyInList\s*\(\s*"kill_switch_active"\s*\)',
        "kill-switch response must explicitly be false": r'JsonFalse\s*\(\s*"kill_switch_active"\s*\)',
        "command fetch fail-closed status": r"if\s*\(\s*code\s*!=\s*200\s*\)",
        "command fetch empty-body guard": r"StringLen\s*\(\s*body\s*\)\s*<\s*10",
        "live-path remains blocked": r"if\s*\(\s*!ENABLE_DEMO_EXECUTION\s*\|\|\s*AllowLiveExecution\s*\)",
        # Parser and slots.
        "strict flat-object JSON validation": r"bool\s+ValidateFlatObject\s*\(",
        # The single-brace-pair check up front is what makes the two in-loop
        # `i >= n` branches unreachable; pin it so the parity exemption stays honest.
        "flat object requires exactly one brace pair": (
            r"if\s*\(\s*n\s*<\s*2\s*\|\|\s*StringGetCharacter\s*\(\s*s\s*,\s*0\s*\)\s*!=\s*'\{'\s*\|\|\s*"
            r"StringGetCharacter\s*\(\s*s\s*,\s*n\s*-\s*1\s*\)\s*!=\s*'\}'\s*\)"
        ),
        "duplicate JSON key guard": (
            r"if\s*\(\s*KeyInList\s*\(\s*key\s*\)\s*\)\s*\{\s*error\s*=\s*\"DUPLICATE JSON KEY: \"\s*\+\s*key\s*;"
            r"\s*return\s+false;\s*\}"
        ),
        "validated key slots declared": r"string\s+g_validated_keys\[\]",
        "validated value slots declared": r"string\s+g_validated_values\[\]",
        "validated type slots declared": r"int\s+g_validated_types\[\]",
        # Slot type 3 is the literal `true`, 4 is the literal `false`; collapsing the ternary
        # would make JsonFalse("kill_switch_active") accept a literal true (and JsonTrue accept
        # false), so the whole return expression is pinned, not just its left-hand side.
        "JsonBoolExact resolves the exact type slots": (
            r"return\s+g_validated_types\[index\]\s*==\s*\(\s*wanted\s*\?\s*3\s*:\s*4\s*\)\s*;"
        ),
        # The wrappers are the only callers that pin the wanted polarity for
        # user_approved / kill_switch_active, so their complete bodies are pinned here.
        "JsonTrue wrapper calls JsonBoolExact with true": (
            r"bool\s+JsonTrue\s*\(\s*const\s+string\s+key\s*\)\s*\{\s*"
            r"return\s+JsonBoolExact\s*\(\s*key\s*,\s*true\s*\)\s*;\s*\}"
        ),
        "JsonFalse wrapper calls JsonBoolExact with false": (
            r"bool\s+JsonFalse\s*\(\s*const\s+string\s+key\s*\)\s*\{\s*"
            r"return\s+JsonBoolExact\s*\(\s*key\s*,\s*false\s*\)\s*;\s*\}"
        ),
        # The parser writes the same mapping; swapping it would make JsonFalse accept an active
        # kill switch (a literal true stored as slot type 4).
        "parser stores true as slot type 3 and false as slot type 4": (
            r"StoreValidatedValue\s*\(\s*key\s*,\s*token\s*,\s*c\s*==\s*'t'\s*\?\s*3\s*:\s*4\s*\)"
        ),
        "validated slot lookup helper": r"\bint\s+FindValidatedKey\s*\(",
        "validated slot store helper": r"\bvoid\s+StoreValidatedValue\s*\(",
        "strict RFC-8259 number token": r"\bbool\s+IsJsonNumberToken\s*\(",
        "number literal finiteness check": r"MathIsValidNumber\s*\(\s*StringToDouble\s*\(",
        "number token rejects a leading zero": (
            r"if\s*\(\s*i\s*-\s*int_start\s*>\s*1\s*&&\s*StringGetCharacter\s*\(\s*text\s*,\s*int_start\s*\)\s*==\s*'0'\s*\)"
        ),
        "complete boolean literal required": (
            r"if\s*\(\s*i\s*\+\s*tl\s*>=\s*n\s*\|\|\s*StringSubstr\s*\(\s*s\s*,\s*i\s*,\s*tl\s*\)\s*!=\s*token\s*\)"
        ),
        "identifier safety check": r"\bbool\s+IsSafeIdentifier\s*\(",
        "identifier length bounded": r"if\s*\(\s*n\s*<\s*1\s*\|\|\s*n\s*>\s*128\s*\)\s*return\s+false\s*;",
        "identifier alphabet pinned": (
            r"bool\s+allowed\s*=\s*\(\s*c\s*>=\s*'0'\s*&&\s*c\s*<=\s*'9'\s*\)\s*\|\|\s*"
            r"\(\s*c\s*>=\s*'A'\s*&&\s*c\s*<=\s*'Z'\s*\)\s*\|\|\s*"
            r"\(\s*c\s*>=\s*'a'\s*&&\s*c\s*<=\s*'z'\s*\)\s*\|\|\s*"
            r"c\s*==\s*'-'\s*\|\|\s*c\s*==\s*'_'\s*\|\|\s*c\s*==\s*'\.'\s*\|\|\s*c\s*==\s*':'\s*;"
        ),
        "command identifier validated": r"IsSafeIdentifier\s*\(\s*command_id\s*\)",
        "order identifier validated": r"IsSafeIdentifier\s*\(\s*order_id\s*\)",
        "symbol identifier validated": r"IsSafeIdentifier\s*\(\s*symbol\s*\)",
        "direction validated before use": r'if\(direction != "BUY" && direction != "SELL"\)',
        "required command keys enforced": r"if\s*\(\s*!\s*KeyInList\s*\(\s*required_keys\[k\]\s*\)\s*\)",
        "required key loop bounded by the array size": (
            r"for\s*\(\s*int\s+k\s*=\s*0\s*;\s*k\s*<\s*ArraySize\s*\(\s*required_keys\s*\)\s*;\s*k\+\+\s*\)"
        ),
        # URL and token validation.
        "strict loopback URL parser": r"\bbool\s+IsLoopbackHttpUrl\s*\(",
        "URL minimum length": r"if\s*\(\s*n\s*<\s*17\s*\)",
        "URL must be plain http": r'StringSubstr\s*\(\s*url\s*,\s*0\s*,\s*7\s*\)\s*!=\s*"http://"',
        "URL printable ASCII only": r"if\s*\(\s*c\s*<\s*0x21\s*\|\|\s*c\s*>\s*0x7E\s*\)",
        "URL userinfo separator rejected": r"c\s*==\s*'@'\s*\|\|\s*c\s*==\s*'#'\s*\|\|\s*c\s*==\s*'\?'\s*\|\|\s*c\s*==\s*'\\\\'",
        "URL must have an explicit port": r'StringFind\s*\(\s*url\s*,\s*":"\s*,\s*host_start\s*\)',
        "URL missing port rejected": r"if\s*\(\s*colon\s*<\s*0\s*\)",
        "exact loopback host allowlist": r"if\s*\(\s*host\s*!=\s*\"127\.0\.0\.1\"\s*&&\s*host\s*!=\s*\"localhost\"\s*\)",
        "URL port text is 1..5 characters": r"if\s*\(\s*plen\s*<\s*1\s*\|\|\s*plen\s*>\s*5\s*\)",
        "URL port leading zero rejected": r"if\s*\(\s*StringGetCharacter\s*\(\s*port_text\s*,\s*0\s*\)\s*==\s*'0'\s*\)",
        "URL port must be numeric": r'"URL port must be numeric"',
        "URL port digits are checked individually": r"if\s*\(\s*c\s*<\s*'0'\s*\|\|\s*c\s*>\s*'9'\s*\)",
        "URL port range checked": r"port\s*<\s*1\s*\|\|\s*port\s*>\s*65535",
        "URL port accumulates decimal digits": r"port\s*=\s*port\s*\*\s*10\s*\+\s*\(\s*c\s*-\s*'0'\s*\)",
        "URL empty path rejected": r"if\s*\(\s*path\s*>=\s*0\s*&&\s*path\s*==\s*n\s*-\s*1\s*\)",
        "bridge token format validation": r"\bbool\s+IsValidBridgeToken\s*\(",
        "token minimum length is 24": r"if\s*\(\s*n\s*<\s*24\s*\|\|\s*n\s*>\s*256\s*\)\s*return\s+false\s*;",
        "token alphabet pinned": (
            r"bool\s+allowed\s*=\s*\(\s*c\s*>=\s*'0'\s*&&\s*c\s*<=\s*'9'\s*\)\s*\|\|\s*"
            r"\(\s*c\s*>=\s*'A'\s*&&\s*c\s*<=\s*'Z'\s*\)\s*\|\|\s*"
            r"\(\s*c\s*>=\s*'a'\s*&&\s*c\s*<=\s*'z'\s*\)\s*\|\|\s*"
            r"c\s*==\s*'\.'\s*\|\|\s*c\s*==\s*'_'\s*\|\|\s*c\s*==\s*'~'\s*\|\|\s*c\s*==\s*'-'\s*;"
        ),
        "bridge token charset is narrow": r"c\s*==\s*'\.'\s*\|\|\s*c\s*==\s*'_'\s*\|\|\s*c\s*==\s*'~'\s*\|\|\s*c\s*==\s*'-'\s*;",
        "token validated before building headers": r"if\s*\(\s*!IsValidBridgeToken\s*\(\s*BridgeToken\s*\)\s*\)",
        # OnInit / OnTimer.
        "valid MQL5 OnInit signature": r"\bint\s+OnInit\s*\(\s*\)",
        "OnInit success return": r"return\s*\(\s*INIT_SUCCEEDED\s*\)",
        "OnInit failure return": r"return\s*\(\s*INIT_FAILED\s*\)",
        "OnTimer checks the kill switch first": r"if\s*\(\s*!CheckKillSwitch\s*\(\s*\)\s*\)\s*return\s*;",
        "OnTimer fetches before executing": r"if\s*\(\s*!FetchCommand\s*\(\s*body\s*\)\s*\)\s*return\s*;",
        "timer started": r"EventSetTimer\s*\(",
    }


def _forbidden_checks() -> dict[str, str]:
    return {
        "preprocessor directive outside #property": (
            r"(?m)^\s*#\s*(?:define|include|import|undef|ifdef|ifndef|elif|endif|pragma|error|line|warning)\b"
        ),
        "dead conditional branch": r"if\s*\(\s*false\s*[)&|]",
        "dead conditional branch (zero)": r"if\s*\(\s*0\s*\)",
        "dead numeric comparison": r"if\s*\(\s*\d+\s*==\s*\d+",
        "dead loop": r"(?:while\s*\(\s*false\s*\)|for\s*\([^;]*;\s*false\s*;)",
        "condition disabled with && false": r"&&\s*false\s*\)",
        "condition forced with || true": r"\|\|\s*true\s*\)",
        "condition forced with true": r"if\s*\(\s*true\s*\)",
        "identifier alphabet weakened with a negation": r"if\s*\(\s*!\s*allowed\s*&&",
        "approval flag reassigned outside its declaration": r"(?m)^[ \t]*user_approved\s*=",
        # Same-line reassignment is caught by the token-level structural check too, so the
        # line-anchored form above is only the first line of defence.
        "extra URL input variable declared": (
            r'(?m)^[ \t]*input\s+string\s+(?!BridgeUrl\b|KillSwitchUrl\b)\w+\s*=\s*"http'
        ),
        "expiry clock shifted away from the current time": r"TimeGMT\s*\(\s*\)\s*[-+]",
        "return filling fallback restored": r"ORDER_FILLING_RETURN",
        "truncating idempotency key restored": r"SanitizeKeyPart|IdempotencyKey",
        "void OnInit signature restored": r"^void\s+OnInit",
        "demo account comparison inverted": r"AccountInfoInteger\s*\(\s*ACCOUNT_TRADE_MODE\s*\)\s*==\s*ACCOUNT_TRADE_MODE_DEMO",
        "loopback allowlist widened with an extra host": r"\|\|\s*host\s*==\s*\"",
        "loopback host checked by prefix": r"StringFind\s*\(\s*(?:BridgeUrl|KillSwitchUrl)\s*,\s*\"http://127\.0\.0\.1:\"\s*\)\s*!=\s*0",
        "kill switch treated as active": r"if\s*\(\s*JsonTrue\s*\(\s*\"kill_switch_active\"\s*\)\s*\)",
        "broker preflight result ignored": r"(?m)^[ \t]*OrderCheck\s*\(",
        # GlobalVariablesFlush() is void in the installed compiler; using it as a value is a
        # compile error, so a source that gates the send on it cannot build at all.
        "void flush used as a value": r"!\s*GlobalVariablesFlush|GlobalVariablesFlush\s*\(\s*\)\s*(?:==|!=)",
        "idempotency flush made conditional": r"if\s*\([^\n]*\)\s*GlobalVariablesFlush\s*\(\s*\)\s*;",
        "write request introduced": r"WebRequest\s*\(\s*\"(?:POST|PUT|PATCH|DELETE)\"",
        "asynchronous or direct order API": (
            r"OrderSendAsync|OrderClose\s*\(|OrderModify\s*\(|OrderDelete\s*\(|PositionClose\s*\(|"
            r"PositionModify\s*\(|PositionOpen\s*\("
        ),
        "notification side channel": r"\bSendNotification\s*\(|\bSendMail\s*\(",
    }


def _required_failures(source: str) -> list[str]:
    return [name for name, pattern in _required_checks().items()
            if re.search(pattern, source, re.M) is None]


def _forbidden_failures(source: str) -> list[str]:
    return [name for name, pattern in _forbidden_checks().items()
            if re.search(pattern, source, re.M) is not None]


def _function_body(source: str, name: str, return_type: str = "bool") -> str | None:
    match = re.search(rf"{return_type}\s+{re.escape(name)}\s*\([^)]*\)\s*\{{(?P<body>.*?)\n\}}", source, re.S)
    return match.group("body") if match else None


def _after(haystack: str, first: str, second: str) -> bool:
    """True when both fragments exist and `first` appears before `second`."""
    return first in haystack and second in haystack and haystack.index(first) < haystack.index(second)


_WS_RE = re.compile(r"\s+")


def _squeeze(text: str) -> str:
    """Whitespace-insensitive form of a fragment, for comparing statements not substrings."""
    return _WS_RE.sub("", text)


def _match_delimiter(source: str, start: int, opening: str, closing: str) -> int:
    """Index of the delimiter closing ``source[start]``; string and char literals are skipped."""
    depth = 0
    i = start
    n = len(source)
    while i < n:
        ch = source[i]
        if ch in "\"'":
            quote = ch
            i += 1
            while i < n:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _if_block_keys(source: str) -> list[str]:
    """Whitespace-free form of every complete ``if (<cond>) { <body> }`` statement.

    Pinning a whole statement instead of a sub-expression is what stops a branch from being
    neutralised while its message and predicates stay in the source. Only a braced body is
    captured; a braceless ``if`` cannot satisfy any of the pinned blocks.
    """
    keys: list[str] = []
    for match in re.finditer(r"\bif\s*\(", source):
        open_paren = source.find("(", match.start())
        close_paren = _match_delimiter(source, open_paren, "(", ")")
        if close_paren < 0:
            continue
        brace = close_paren + 1
        while brace < len(source) and source[brace].isspace():
            brace += 1
        if brace >= len(source) or source[brace] != "{":
            continue
        close_brace = _match_delimiter(source, brace, "{", "}")
        if close_brace < 0:
            continue
        keys.append("if(" + _squeeze(source[open_paren + 1:close_paren]) + "){"
                    + _squeeze(source[brace + 1:close_brace]) + "}")
    return keys


def _has_if_block(blocks: list[str], expected: str) -> bool:
    """True when ``expected`` matches one complete ``if`` statement, layout and comments aside."""
    return _squeeze(expected) in blocks


def _pinned_guard(scope: str, blocks: list[str], expected: str, marker: str | None = None,
                  anchor: str = "first") -> bool:
    """A guard must appear exactly once in ``scope``, and before ``marker`` when given.

    ``_has_if_block`` alone only proves that the statement exists *somewhere*: deleting the
    real guard and appending an unreachable copy after the success return keeps the text but
    leaves the path unguarded. Counting the exact statement inside the function and requiring
    it to precede the send (or the last success return) closes that gap.
    """
    if not _has_if_block(blocks, expected):
        return False
    key = _squeeze(expected)
    squashed = _squeeze(scope)
    if not key or squashed.count(key) != 1:
        return False
    if marker is None:
        return True
    marker_key = _squeeze(marker)
    position = squashed.rfind(marker_key) if anchor == "last" else squashed.find(marker_key)
    return position >= 0 and squashed.find(key) < position


def _flat_function_body(source: str, name: str, return_type: str = "bool") -> str | None:
    """Body of a function whose braces are not nested, e.g. the one-line Json wrappers."""
    match = re.search(rf"\b{re.escape(return_type)}\s+{name}\s*\([^)]*\)\s*\{{([^{{}}]*)\}}",
                      source)
    return match.group(1) if match else None


def _mask_string_literals(source: str) -> str:
    """Blank out string/char literal contents, preserving offsets, so literals cannot match."""
    out: list[str] = []
    i = 0
    n = len(source)
    quote = ""
    while i < n:
        ch = source[i]
        if not quote:
            out.append(ch)
            if ch in "\"'":
                quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append("  ")
            i += 2
            continue
        if ch == quote:
            out.append(ch)
            quote = ""
        else:
            out.append(" ")
        i += 1
    return "".join(out)


def _parenthesised_literal_terms(source: str) -> list[str]:
    """Literal terms that neutralise or force a condition, e.g. ``... && (false)`` / ``&& 0``.

    A whole control condition such as ``while(true)`` is left to the dedicated dead-branch
    checks; a literal wrapped in parentheses anywhere inside an expression is a bypass, because
    it neutralises (or forces) the surrounding conjunction/disjunction while every required
    sub-expression stays textually present. Bare numeric/boolean terms after ``&&``/``||`` are
    the same bypass without the parentheses. String literals are masked first so a message that
    mentions ``(false)`` cannot trip the check.
    """
    masked = _mask_string_literals(source)
    terms: list[str] = []
    for match in re.finditer(r"\(\s*(?:true|false|0|1)\s*\)", masked):
        before = masked[:match.start()].rstrip()
        after = masked[match.end():].lstrip()
        whole_condition = (
            (before.endswith("if") or before.endswith("while") or before.endswith("for")
             or before.endswith("else"))
            and (after.startswith("{") or after.startswith(";") or after == "")
        )
        if not whole_condition:
            terms.append(match.group(0))
    for match in re.finditer(r"(?:&&|\|\|)\s*(?:true|false|0|1)\b", masked):
        terms.append(match.group(0))
    return terms


def _guard_body_checks(source: str) -> list[tuple[str, bool]]:
    """Every BLOCKED branch must return by itself, not merely log.

    A bare ``Print("BLOCKED: ...")`` without ``return false;`` is the cheapest way to
    disable a check while keeping the message, so the return is required in the same
    brace block as the message.
    """
    checks: list[tuple[str, bool]] = []
    pattern = re.compile(r'Print\s*\(\s*"(BLOCKED[^"]*|DUPLICATE BLOCKED[^"]*)"')
    guards = 0
    for match in pattern.finditer(source):
        guards += 1
        semi = _scan_to(source, match.end(), ";")
        close = _scan_to(source, semi + 1, "}") if semi >= 0 else -1
        after = source[semi + 1:close] if 0 <= semi < close else ""
        ok = "return false;" in after or "return(INIT_FAILED);" in after
        checks.append((f"guarded block must return false: {match.group(1)}", ok))
    checks.append(("at least 30 guarded block branches are present", guards >= 30))
    return checks


def _structural_checks(source: str) -> list[tuple[str, bool]]:
    checks: list[tuple[str, bool]] = []
    send = "OrderSend(request, result)"
    blocks = _if_block_keys(source)
    if send not in source:
        return [("expected broker send function is absent", False)]
    checks.append(("exactly one broker OrderSend call", source.count("OrderSend(") == 1))
    checks.append(("exactly one broker OrderCheck call", source.count("OrderCheck(") == 1))
    checks.append(("exactly two GET web requests",
                   source.count('WebRequest("GET"') == 2 and source.count("WebRequest(") == 2))
    web_targets = re.findall(r'WebRequest\s*\(\s*"GET"\s*,\s*([A-Za-z_]\w*)\s*,', source)
    checks.append(("web requests pass only the two validated URL inputs",
                   sorted(web_targets) == ["BridgeUrl", "KillSwitchUrl"]))
    checks.append(("the expiry clock is assigned exactly once",
                   len(re.findall(r"\bnow_epoch\s*(?:[-+*/%&|^]|<<|>>)?=(?!=)", source)) == 1))
    checks.append(("FNV multiply constant appears twice", source.count("hash *= 1099511628211;") == 2))
    checks.append(("OrderCheck precedes OrderSend", _after(source, "OrderCheck(request, check)", send)))
    checks.append(("command tombstone precedes OrderSend", _after(source, "GlobalVariableSet(command_key", send)))
    checks.append(("order tombstone precedes OrderSend", _after(source, "GlobalVariableSet(order_key", send)))
    checks.append(("tombstone flush precedes OrderSend", _after(source, "GlobalVariablesFlush()", send)))
    checks.append(("duplicate key rejection is part of the parser",
                   _after(source, "DUPLICATE JSON KEY", "DUPLICATE BLOCKED")))
    checks.append(("demo account check precedes OrderSend",
                   _after(source, "ACCOUNT_TRADE_MODE_DEMO", send)))
    checks.append(("demo/live gate precedes OrderSend",
                   _after(source, "if(!ENABLE_DEMO_EXECUTION || AllowLiveExecution)", send)))
    checks.append(("symbol trading mode precedes OrderSend",
                   _after(source, "SYMBOL_TRADE_MODE_FULL", send)))
    checks.append(("slippage guard precedes OrderSend",
                   _after(source, "MaxSlippagePoints * point", send)))
    checks.append(("broker stops level precedes OrderSend",
                   _after(source, "SYMBOL_TRADE_STOPS_LEVEL", send)))
    checks.append(("filling mode precedes OrderSend",
                   _after(source, "request.type_filling", send)))

    definitions = (("OnInit", "int"), ("OnTimer", "void"), ("OnDeinit", "void"),
                   ("CheckKillSwitch", "bool"), ("FetchCommand", "bool"),
                   ("ExecuteDemoCommand", "bool"), ("ValidateFlatObject", "bool"),
                   ("IsLoopbackHttpUrl", "bool"), ("IsValidBridgeToken", "bool"),
                   ("IsSafeIdentifier", "bool"), ("IsJsonNumberToken", "bool"),
                   ("JsonString", "string"), ("JsonNumber", "double"), ("JsonBoolExact", "bool"),
                   ("JsonTrue", "bool"), ("JsonFalse", "bool"),
                   ("FindValidatedKey", "int"), ("StoreValidatedValue", "void"),
                   ("CommandTombstoneKey", "string"), ("OrderTombstoneKey", "string"),
                   ("Fnv1aHex", "string"))
    for name, return_type in definitions:
        found = re.findall(rf"(?m)^\s*{return_type}\s+{name}\s*\(", source)
        checks.append((f"{name} is defined exactly once", len(found) == 1))

    # Each function must open with its real first statement, so a prepended
    # `return true;` cannot turn the whole function into a no-op.
    first_fragments = {
        "CheckKillSwitch": "if(!IsValidBridgeToken(BridgeToken) || StringLen(KillSwitchUrl) == 0)",
        "FetchCommand": "char data[];",
        "ExecuteDemoCommand": 'string parse_error = "";',
        "ValidateFlatObject": 'error = "";',
        "IsSafeIdentifier": "int n = StringLen(value);",
        "IsValidBridgeToken": "int n = StringLen(token);",
        "IsLoopbackHttpUrl": 'error = "";',
        "IsJsonNumberToken": "int n = StringLen(text);",
        "JsonString": "int index = FindValidatedKey(key);",
        "JsonNumber": "int index = FindValidatedKey(key);",
        "JsonBoolExact": "int index = FindValidatedKey(key);",
        "Fnv1aHex": "ulong hash = ((ulong)0xCBF29CE4 << 32) | (ulong)0x84222325;",
        "OnTimer": "if(!CheckKillSwitch()) return;",
    }
    return_types = {name: return_type for name, return_type in definitions}
    for name, fragment in first_fragments.items():
        body = _function_body(source, name, return_types[name])
        checks.append((f"{name} starts with its guard", body is not None and body.lstrip().startswith(fragment)))

    parser = _function_body(source, "ValidateFlatObject")
    checks.append(("ValidateFlatObject function present", parser is not None))
    if parser is not None:
        for slot in ("g_validated_keys", "g_validated_values", "g_validated_types"):
            checks.append((f"ValidateFlatObject resets {slot}", f"ArrayResize({slot}, 0)" in parser))
        # Exactly three slots are stored (string, number, boolean) and each is keyed by the
        # parsed `key` variable. A literal key such as StoreValidatedValue("user_approved", ...)
        # would forge that field in every response, so the count and the first argument are pinned.
        slot_calls = re.findall(r"StoreValidatedValue\s*\(", parser)
        keyed_calls = re.findall(r"StoreValidatedValue\s*\(\s*key\s*,", parser)
        checks.append(("ValidateFlatObject stores exactly three validated values",
                       len(slot_calls) == 3))
        checks.append(("ValidateFlatObject keys every stored value by the parsed key",
                       len(keyed_calls) == 3))
        checks.append(("validated slots are never stored under a literal key",
                       re.search(r'StoreValidatedValue\s*\(\s*"', source) is None))
        checks.append(("validated slots are stored only by the parser",
                       len(re.findall(r"StoreValidatedValue\s*\(", source)) == 4))
        # StoreValidatedValue appends the key itself. A hand-written append beside it leaves
        # g_validated_keys one slot ahead of g_validated_values/types, so every lookup resolves
        # the empty placeholder and the EA silently misreads every response.
        checks.append(("validated key slots are appended only by StoreValidatedValue",
                       "ArrayResize(g_validated_keys, ArraySize(" not in source))
        checks.append(("duplicate key rejection is inside ValidateFlatObject",
                       "DUPLICATE JSON KEY" in parser))
        checks.append(("number values use the strict token parser",
                       "IsJsonNumberToken(number_token)" in parser))
        checks.append(("nested objects and arrays are rejected",
                       parser.count("nested") >= 2))
        checks.append(("escapes are rejected", "escape sequences rejected" in parser))
        checks.append(("control characters are rejected", "control character rejected" in parser))
        checks.append(("trailing content is rejected", "trailing content after object" in parser))
        checks.append(("boolean literals are matched exactly",
                       'StringSubstr(s, i, tl) != token' in parser))
        checks.append(("keys are trimmed only around the whole object",
                       "StringTrimLeft(s);" in parser and "StringTrimRight(s);" in parser))
        checks.append(("ValidateFlatObject has exactly two success returns",
                       parser.count("return true;") == 2))
        checks.append(("every parser acceptance requires the object to end exactly here",
                       parser.count("if(i != n-1)") == 2))
        checks.append(("the parser acceptance after a value follows the duplicate-key guard",
                       "DUPLICATE JSON KEY" in parser
                       and parser.rindex("return true;") > parser.index("DUPLICATE JSON KEY")))
        # Presence of the message is not enforcement: each rejection must still be a whole
        # `if (<predicate>) { error = "..."; return false; }` statement that sits on the
        # reachable path, i.e. before the parser's last success return.
        checks.append(("parser rejects nested values in a complete fail-closed branch",
                       _pinned_guard(parser, blocks, """if(c == '{' || c == '[') { error = "nested value rejected"; return false; }""",
                                     "return true;", anchor="last")))
        checks.append(("parser rejects key escapes in a complete fail-closed branch",
                       _pinned_guard(parser, blocks, r"""if(kc == '\\') { error = "escape sequences rejected"; return false; }""",
                                     "return true;", anchor="last")))
        checks.append(("parser rejects value escapes in a complete fail-closed branch",
                       _pinned_guard(parser, blocks, r"""if(vc == '\\') { error = "escape sequences rejected"; return false; }""",
                                     "return true;", anchor="last")))
        checks.append(("parser rejects key control characters in a complete fail-closed branch",
                       _pinned_guard(parser, blocks, """if(kc < 0x20) { error = "control character rejected"; return false; }""",
                                     "return true;", anchor="last")))
        checks.append(("parser rejects value control characters in a complete fail-closed branch",
                       _pinned_guard(parser, blocks, """if(vc < 0x20) { error = "control character rejected"; return false; }""",
                                     "return true;", anchor="last")))

    lookups = {
        "JsonString": ("string", "return g_validated_values[index];", 1),
        "JsonNumber": ("double", "g_validated_values[index]", 2),
        "JsonBoolExact": ("bool", "return g_validated_types[index] == (wanted ? 3 : 4);", None),
    }
    for name, (return_type, needle, slot_type) in lookups.items():
        body = _function_body(source, name, return_type)
        checks.append((f"{name} function present", body is not None))
        if body is None:
            continue
        checks.append((f"{name} reads validated slots", needle in body))
        checks.append((f"{name} does not re-parse the raw body",
                       "StringFind(" not in body and "StringSubstr(" not in body))
        checks.append((f"{name} looks up the exact key", "FindValidatedKey(key)" in body))
        if slot_type is not None:
            checks.append((f"{name} enforces its value type",
                           f"g_validated_types[index] != {slot_type}" in body))

    # The polarity of user_approved and kill_switch_active lives entirely in these two
    # wrappers. Their whole body is compared, so flipping the wanted literal is caught.
    for name, wanted in (("JsonTrue", "true"), ("JsonFalse", "false")):
        body = _flat_function_body(source, name)
        checks.append((f"{name} is exactly one JsonBoolExact(key, {wanted}) call",
                       body is not None
                       and _squeeze(body) == f"returnJsonBoolExact(key,{wanted});"))

    command = _function_body(source, "ExecuteDemoCommand")
    checks.append(("ExecuteDemoCommand function present", command is not None))
    if command is not None:
        checks.append(("command body is validated before field extraction",
                       _after(command, "ValidateFlatObject(body", 'JsonString("command_id")')))
        checks.append(("required keys are checked before field extraction",
                       _after(command, "KeyInList(required_keys[k])", 'JsonString("command_id")')))
        checks.append(("identifiers are validated before use",
                       _after(command, "IsSafeIdentifier(command_id)", "live_price =")))
        checks.append(("direction is validated before it is used",
                       _after(command, 'direction != "BUY"', "live_price =")))
        checks.append(("demo/approval gate precedes broker checks",
                       _after(command, "decision != \"APPROVE\"", "SymbolSelect(symbol")))
        checks.append(("kill switch is re-read after OrderCheck",
                       _after(command, "OrderCheck(request, check)", "CheckKillSwitch()")))
        checks.append(("kill switch re-read precedes the tombstone write",
                       _after(command, "CheckKillSwitch()", "GlobalVariableSet(command_key")))
        checks.append(("duplicate tombstone check precedes broker preflight",
                       _after(command, "GlobalVariableCheck(command_key)", "OrderCheck(request, check)")))
        checks.append(("OrderCheck precedes the tombstone write",
                       _after(command, "OrderCheck(request, check)", "GlobalVariableSet(command_key")))
        checks.append(("tombstones are written before the flush",
                       _after(command, "GlobalVariableSet(order_key", "GlobalVariablesFlush()")))
        # The duplicate guard earlier in the function is not this check: the persisted keys
        # must be re-read after both writes, before the send.
        write_at = command.find("GlobalVariableSet(order_key")
        verify_at = command.find("GlobalVariableCheck(command_key)", write_at) \
            if write_at >= 0 else -1
        checks.append(("tombstones are re-checked after they are written",
                       write_at >= 0 and verify_at > write_at))
        # GlobalVariablesFlush() returns void in build 6295; gating on it cannot compile.
        checks.append(("the void flush is not used as a value",
                       "!GlobalVariablesFlush" not in source
                       and "GlobalVariablesFlush() ==" not in source
                       and "GlobalVariablesFlush() !=" not in source))
        checks.append(("the flush precedes OrderSend",
                       _after(command, "GlobalVariablesFlush()", "OrderSend(request, result)")))
        checks.append(("the last order gate is the kill-switch re-read",
                       "CheckKillSwitch()" in command and send in command
                       and command.rindex("CheckKillSwitch()") < command.rindex(send)))
        checks.append(("exactly one kill-switch re-read in the command path",
                       command.count("CheckKillSwitch()") == 1))
        checks.append(("the send result is checked", "if(!sent || (result.retcode" in command))
        # The stops are validated as locals; they must also be the values handed to the
        # broker, exactly once each, before the send.
        checks.append(("exactly one protective stop-loss assignment to the request",
                       len(re.findall(r"request\.sl\s*=(?!=)", source)) == 1))
        checks.append(("exactly one protective take-profit assignment to the request",
                       len(re.findall(r"request\.tp\s*=(?!=)", source)) == 1))
        checks.append(("exactly one volume assignment to the request",
                       len(re.findall(r"request\.volume\s*=(?!=)", source)) == 1))
        checks.append(("exactly one order-type assignment to the request",
                       len(re.findall(r"request\.type\s*=(?!=)", source)) == 1))
        checks.append(("protective stops are bound before the send",
                       _after(command, "request.sl = NormalizeDouble(sl, digits);", send)
                       and _after(command, "request.tp = NormalizeDouble(tp, digits);", send)))
        checks.append(("validated volume is bound before the send",
                       _after(command, "request.volume = volume;", send)))
        checks.append(("validated direction selects the type before the send",
                       _after(command, 'request.type = (direction=="BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;',
                              send)))
        # ...and in the same straight-line run as the other request fields, so they cannot be
        # moved inside a conditional that the normal path never enters.
        checks.append(("protective stops sit in the unconditional request block",
                       _squeeze("request.volume = volume;"
                                'request.type = (direction=="BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;'
                                "request.price = NormalizeDouble(live_price, digits);"
                                "request.sl = NormalizeDouble(sl, digits);"
                                "request.tp = NormalizeDouble(tp, digits);"
                                "request.deviation = MaxSlippagePoints;") in _squeeze(command)))
        # The approval flag is a one-shot derived value: any second assignment token, on any
        # line and anywhere in the source, would let a caller force approval after validation.
        checks.append(("approval flag is assigned only in its single declaration",
                       len(re.findall(r"\buser_approved\s*(?:[-+*/%&|^]|<<|>>)?=(?!=)", source)) == 1
                       and len(re.findall(r"\bbool\s+user_approved\s*=(?!=)", source)) == 1))
        # Every guard must be the one on the live path, before the send.
        checks.append(("the persisted tombstones are verified before the send",
                       _pinned_guard(command, blocks, 'if(!GlobalVariableCheck(command_key) || !GlobalVariableCheck(order_key)) '
                                                      '{ Print("BLOCKED: idempotency tombstones missing after persist."); return false; }',
                                     "bool sent = OrderSend(request, result);")))
        # The predicates alone are not enforcement: each gate must still be the complete
        # statement whose body prints and returns, in one piece, ahead of the send.
        checks.append(("user approval gate is a complete fail-closed branch",
                       _pinned_guard(command, blocks, 'if(mode != "demo" || decision != "APPROVE" || !user_approved) '
                                                      '{ Print("BLOCKED: demo mode and explicit user approval are required."); return false; }',
                                     "bool sent = OrderSend(request, result);")))
        checks.append(("identifier validation is a complete fail-closed branch",
                       _pinned_guard(command, blocks, 'if(!IsSafeIdentifier(command_id) || !IsSafeIdentifier(order_id) '
                                                      '|| !IsSafeIdentifier(symbol)) '
                                                      '{ Print("BLOCKED: command/order/symbol identifier contains unsupported characters."); return false; }',
                                     "bool sent = OrderSend(request, result);")))
        checks.append(("volume, SL, TP and expected price guard is a complete fail-closed branch",
                       _pinned_guard(command, blocks, 'if(volume <= 0 || volume > MaxVolume || sl <= 0 '
                                                      '|| tp <= 0 || expected_price <= 0) '
                                                      '{ Print("BLOCKED: invalid volume/SL/TP/expected price."); return false; }',
                                     "bool sent = OrderSend(request, result);")))
        checks.append(("order comment is bounded", "StringSubstr(order_id,0,20)" in command))
        checks.append(("ExecuteDemoCommand has exactly one success return",
                       command.count("return true;") == 1))
        checks.append(("the command success return follows OrderSend",
                       command.count("return true;") == 1 and send in command
                       and command.index("return true;") > command.index(send)))

    timer = _function_body(source, "OnTimer", "void")
    checks.append(("OnTimer function present", timer is not None))
    if timer is not None:
        checks.append(("kill switch is checked before command polling",
                       _after(timer, "if(!CheckKillSwitch()) return;", "if(!FetchCommand(body)) return;")))
        checks.append(("command polling precedes execution",
                       _after(timer, "if(!FetchCommand(body)) return;", "ExecuteDemoCommand(body);")))

    kill = _function_body(source, "CheckKillSwitch")
    checks.append(("CheckKillSwitch function present", kill is not None))
    if kill is not None:
        checks.append(("kill-switch body is validated as a flat object first",
                       _after(kill, "ValidateFlatObject(response", "JsonFalse")))
        checks.append(("kill-switch field presence precedes the value check",
                       _after(kill, 'KeyInList("kill_switch_active")', 'JsonFalse("kill_switch_active")')))
        checks.append(("kill-switch token is validated before building headers",
                       _after(kill, "IsValidBridgeToken(BridgeToken)", 'headers += "X-Bridge-Key: "')))
        checks.append(("kill-switch HTTP failure is fail-closed",
                       _after(kill, "if(code != 200)", "ValidateFlatObject(response")))
        checks.append(("kill-switch response must contain the active flag",
                       'KeyInList("kill_switch_active")' in kill))
        checks.append(("kill-switch value check is a complete fail-closed branch",
                       _pinned_guard(kill, blocks, 'if(!JsonFalse("kill_switch_active")) '
                                                   '{ Print("BLOCKED: kill switch is active or state is invalid."); return false; }',
                                     "return true;")))
        checks.append(("CheckKillSwitch has exactly one success return", kill.count("return true;") == 1))
        checks.append(("CheckKillSwitch success return follows the active-state check",
                       kill.count("return true;") == 1 and 'JsonFalse("kill_switch_active")' in kill
                       and kill.index("return true;") > kill.index('JsonFalse("kill_switch_active")')))

    fetch = _function_body(source, "FetchCommand")
    checks.append(("FetchCommand function present", fetch is not None))
    if fetch is not None:
        checks.append(("command token is validated before building headers",
                       _after(fetch, "IsValidBridgeToken(BridgeToken)", 'headers += "X-Bridge-Key: "')))
        checks.append(("command fetch rejects non-200 before reading the body",
                       _after(fetch, "if(code != 200)", "body = CharArrayToString(")))
        checks.append(("command fetch treats 204/404 as no command",
                       "code == 204 || code == 404" in fetch))
        checks.append(("FetchCommand has exactly one success return", fetch.count("return true;") == 1))
        checks.append(("FetchCommand success return follows the body-length guard",
                       fetch.count("return true;") == 1 and "StringLen(body) < 10" in fetch
                       and fetch.index("return true;") > fetch.index("StringLen(body) < 10")))

    oninit = _function_body(source, "OnInit", "int")
    checks.append(("OnInit function present", oninit is not None))
    if oninit is not None:
        checks.append(("OnInit validates both loopback URLs", oninit.count("IsLoopbackHttpUrl(") >= 2))
        checks.append(("OnInit fails closed", "return(INIT_FAILED)" in oninit))
        checks.append(("timer is started after endpoint validation",
                       _after(oninit, "IsLoopbackHttpUrl(KillSwitchUrl", "EventSetTimer")))
        checks.append(("OnInit rejects a malformed configured token",
                       _after(oninit, "IsValidBridgeToken(BridgeToken)", "EventSetTimer")))
        checks.append(("OnInit requires a configured bridge token before starting the timer",
                       _after(oninit, "if(!IsValidBridgeToken(BridgeToken))", "EventSetTimer")))
        checks.append(("OnInit succeeds only at the end", oninit.rindex("return(INIT_SUCCEEDED)") > oninit.rindex("EventSetTimer")))

    url = _function_body(source, "IsLoopbackHttpUrl")
    if url is not None:
        checks.append(("URL parser reads the host before the allowlist",
                       _after(url, "string host = StringSubstr", 'host != "127.0.0.1"')))
        checks.append(("URL parser rejects an empty path", 'path == n - 1' in url))
        checks.append(("URL parser rejects leading-zero ports",
                       "URL port must not have a leading zero" in url))
        checks.append(("IsLoopbackHttpUrl has exactly one success return", url.count("return true;") == 1))
        checks.append(("the URL success return follows the port range check",
                       url.count("return true;") == 1 and "port < 1 || port > 65535" in url
                       and url.index("return true;") > url.index("port < 1 || port > 65535")))

    token = _function_body(source, "IsValidBridgeToken")
    checks.append(("IsValidBridgeToken function present", token is not None))
    if token is not None:
        checks.append(("token length is checked before the alphabet",
                       _after(token, "n < 24 || n > 256", "StringGetCharacter(token")))
        checks.append(("IsValidBridgeToken has exactly one success return", token.count("return true;") == 1))
        checks.append(("token success return follows the alphabet rejection",
                       token.count("return true;") == 1 and "if(!allowed) return false;" in token
                       and token.index("return true;") > token.index("if(!allowed) return false;")))

    identifier = _function_body(source, "IsSafeIdentifier")
    if identifier is not None:
        checks.append(("IsSafeIdentifier has exactly one success return",
                       identifier.count("return true;") == 1))
        checks.append(("identifier success return follows the alphabet rejection",
                       identifier.count("return true;") == 1 and "if(!allowed) return false;" in identifier
                       and identifier.index("return true;") > identifier.index("if(!allowed) return false;")))

    number = _function_body(source, "IsJsonNumberToken")
    checks.append(("IsJsonNumberToken accepts only after consuming every character",
                   number is not None and number.rstrip().endswith("return i == n;")))

    fnv = _function_body(source, "Fnv1aHex", "string")
    checks.append(("FNV hashes both bytes per character",
                   fnv is not None and fnv.count("hash *= 1099511628211;") == 2))
    checks.append(("FNV renders 16 uppercase hex digits last",
                   fnv is not None and fnv.rstrip().endswith('return StringFormat("%016I64X", (long)hash);')))

    checks += _guard_body_checks(source)
    literal_terms = _parenthesised_literal_terms(source)
    checks.append(("literal short-circuit term is forbidden", not literal_terms))
    return checks


def _structural_problems(source: str) -> list[str]:
    return [name for name, ok in _structural_checks(source) if not ok]


MUTATIONS: dict[str, tuple[str, str] | tuple[tuple[str, str], ...]] = {
    "kill-switch recheck before send removed": (
        '   if(!CheckKillSwitch())\n   { Print("BLOCKED: kill switch changed before send."); return false; }\n',
        "",
    ),
    "kill-switch recheck commented out": (
        '   if(!CheckKillSwitch())\n   { Print("BLOCKED: kill switch changed before send."); return false; }\n',
        '   /* if(!CheckKillSwitch())\n   { Print("BLOCKED: kill switch changed before send."); return false; } */\n',
    ),
    "token minimum lowered": ("if(n < 24 || n > 256) return false;", "if(n < 1) return false;"),
    "JsonString type guard removed": (
        '   if(index < 0 || g_validated_types[index] != 1) return "";\n',
        "",
    ),
    "JsonString re-parses the raw body": (
        "   return g_validated_values[index];",
        "   return StringSubstr(g_validated_values[index], 0, 0);",
    ),
    "symbol trade mode disabled": ("trade_mode != SYMBOL_TRADE_MODE_FULL", "trade_mode != SYMBOL_TRADE_MODE_DISABLED"),
    "tombstone flush removed": (
        "   GlobalVariablesFlush();\n",
        "",
    ),
    "tombstone verification removed": (
        '   if(!GlobalVariableCheck(command_key) || !GlobalVariableCheck(order_key))\n'
        '   { Print("BLOCKED: idempotency tombstones missing after persist."); return false; }\n',
        "",
    ),
    "tombstone verification short-circuited": (
        "if(!GlobalVariableCheck(command_key) || !GlobalVariableCheck(order_key))",
        "if(false)",
    ),
    "kill-switch URL parser removed": ("if(!IsLoopbackHttpUrl(KillSwitchUrl, url_error))", "if(false)"),
    "terminal trading check short-circuited": (
        "if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))",
        "if(false || !AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))",
    ),
    "demo account comparison inverted": (
        "AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO",
        "AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_DEMO",
    ),
    "user approval hard-coded": ('bool user_approved = JsonTrue("user_approved");', "bool user_approved = true;"),
    "approval gate short-circuited": ('if(mode != "demo" || decision != "APPROVE" || !user_approved)', "if(false)"),
    "return filling fallback restored": (
        'else { Print("BLOCKED: symbol advertises neither FOK nor IOC for a market deal."); return false; }',
        "else request.type_filling = ORDER_FILLING_RETURN;",
    ),
    "filling mode assumed": ("if((filling & (long)SYMBOL_FILLING_FOK) != 0)", "if(true)"),
    "duplicate tombstone check short-circuited": (
        "if(GlobalVariableCheck(command_key) || GlobalVariableCheck(order_key))",
        "if(false)",
    ),
    "expiry whole-number guard removed": (
        "if(expires_raw <= 0 || MathFloor(expires_raw) != expires_raw)",
        "if(expires_raw <= 0)",
    ),
    "expiry grace introduced": ("if(expires_raw <= now_epoch)", "if(expires_raw <= now_epoch - 86400)"),
    "TTL bound widened": ("now_epoch + MaxCommandTtlSeconds", "now_epoch + MaxCommandTtlSeconds * 1000"),
    "volume cap removed": ("volume <= 0 || volume > MaxVolume || sl <= 0", "volume <= 0 || sl <= 0"),
    "slippage threshold widened": (
        "MathAbs(live_price - expected_price) > MaxSlippagePoints * point",
        "MathAbs(live_price - expected_price) > MaxSlippagePoints * 50 * point",
    ),
    "stops level check disabled": (
        "MathAbs(live_price - sl) < stops * point || MathAbs(tp - live_price) < stops * point",
        "false",
    ),
    "volume step rounding removed": (
        "MathAbs(volume/step - MathRound(volume/step)) > 0.000001",
        "false",
    ),
    "duplicate key rejection removed": (
        'error = "DUPLICATE JSON KEY: " + key;',
        "error = key;",
    ),
    "duplicate key guard short-circuited": ("if(KeyInList(key))", "if(1 == 0 && KeyInList(key))"),
    "loopback allowlist widened": (
        'if(host != "127.0.0.1" && host != "localhost")',
        'if(host != "127.0.0.1" && host != "localhost" || host == "evil.example")',
    ),
    "OrderCheck failure branch removed": (
        '   if(!OrderCheck(request, check))\n'
        '   { Print("BLOCKED: OrderCheck failed; retcode=",check.retcode," ",check.comment); return false; }\n',
        "   OrderCheck(request, check);\n",
    ),
    "direction validation removed": ('if(direction != "BUY" && direction != "SELL")', "if(false)"),
    "identifier validation removed": (
        "if(!IsSafeIdentifier(command_id) || !IsSafeIdentifier(order_id) || !IsSafeIdentifier(symbol))",
        "if(false)",
    ),
    "required key guard removed": ("if(!KeyInList(required_keys[k]))", "if(false)"),
    "retcode validation removed": (
        "if(!sent || (result.retcode != TRADE_RETCODE_DONE && result.retcode != TRADE_RETCODE_DONE_PARTIAL && result.retcode != TRADE_RETCODE_PLACED))",
        "if(false)",
    ),
    "kill switch accepts active state": ('if(!JsonFalse("kill_switch_active"))', 'if(!JsonTrue("kill_switch_active"))'),
    "kill-switch presence check removed": ('if(!KeyInList("kill_switch_active"))', "if(false)"),
    "duplicate function definition injected": (
        "bool CheckKillSwitch()\n{",
        "bool CheckKillSwitch()\n{ return true; }\nbool CheckKillSwitch()\n{",
    ),
    # Round-5 bypasses.
    "blocked branch keeps the message but drops the return": (
        '{ Print("BLOCKED: slippage guard triggered."); return false; }',
        '{ Print("BLOCKED: slippage guard triggered."); }',
    ),
    "early success injected into the command path": (
        "bool ExecuteDemoCommand(const string body)\n{\n",
        "bool ExecuteDemoCommand(const string body)\n{\n   return true;\n",
    ),
    "early acceptance injected into the parser": (
        "   int i = 1;\n   bool after_comma = false;\n",
        "   int i = 1;\n   bool after_comma = false;\n   return true;\n",
    ),
    "preprocessor macro injected": (
        "#property strict",
        "#property strict\n#define MaxVolume 1000.0",
    ),
    "second order send injected": (
        "   bool sent = OrderSend(request, result);",
        "   OrderSend(request, result);\n   bool sent = OrderSend(request, result);",
    ),
    "dead parser loop": ("   while(true)\n   {", "   while(false)\n   {"),
    "FNV multiply constant weakened": ("hash *= 1099511628211;", "hash *= 1099511628211 + 1;"),
    "identifier alphabet widened": (
        "|| c == '-' || c == '_' || c == '.' || c == ':'",
        "|| c == '-' || c == '_' || c == '.' || c == ':' || c == '/'",
    ),
    "token alphabet widened": (
        "|| c == '.' || c == '_' || c == '~' || c == '-'",
        "|| c == '.' || c == '_' || c == '~' || c == '-' || c == ':'",
    ),
    "identifier length bound removed": ("if(n < 1 || n > 128) return false;", "if(n < 1) return false;"),
    "URL leading-zero port guard removed": ("if(StringGetCharacter(port_text, 0) == '0')", "if(false)"),
    "URL port range widened": ("port < 1 || port > 65535", "port < 1"),
    "required-key loop bound shrunk": ("k < ArraySize(required_keys)", "k < 5"),
    "boolean prefix accepted": (
        "if(i + tl >= n || StringSubstr(s, i, tl) != token)",
        "if(i + tl > n || StringSubstr(s, i, tl) != token)",
    ),
    "JsonNumber type guard removed": (
        "if(index < 0 || g_validated_types[index] != 2) return 0.0;",
        "if(index < 0) return 0.0;",
    ),
    "write request introduced": ('WebRequest("GET", BridgeUrl', 'WebRequest("POST", BridgeUrl'),
    "max volume default widened": ("input double MaxVolume = 0.20;", "input double MaxVolume = 20.0;"),
    "slippage default widened": ("input int MaxSlippagePoints = 20;", "input int MaxSlippagePoints = 500;"),
    "TTL default widened": ("input double MaxCommandTtlSeconds = 600;", "input double MaxCommandTtlSeconds = 86400;"),
    "kill-switch status check widened": (
        '   if(code != 200)\n   {\n      Print("BLOCKED: kill-switch',
        '   if(code == -1)\n   {\n      Print("BLOCKED: kill-switch',
    ),
    "URL minimum length removed": ("if(n < 17)", "if(n < 1)"),
    "URL printable check weakened": ("if(c < 0x21 || c > 0x7E)", "if(c < 0x01 || c > 0x7F)"),
    "URL userinfo rejection removed": ("if(c == '@' || c == '#' || c == '?' || c == '\\\\')", "if(false)"),
    "number leading-zero guard removed": (
        "if(i - int_start > 1 && StringGetCharacter(text, int_start) == '0')",
        "if(false)",
    ),
    # Round-6 bypasses: parser slot forging, ternary collapse, parenthesised literals and
    # message-only guards.
    "literal-key slot forged inside the parser": (
        "StoreValidatedValue(key, token, c == 't' ? 3 : 4);",
        'StoreValidatedValue(key, token, c == \'t\' ? 3 : 4);\n'
        '         StoreValidatedValue("user_approved", "true", 3);',
    ),
    "parsed key replaced by a literal slot key": (
        "StoreValidatedValue(key, number_token, 2);",
        'StoreValidatedValue("user_approved", "true", 3);',
    ),
    "key slot appended by hand in the parser": (
        "i++; // closing quote of the key",
        "ArrayResize(g_validated_keys, ArraySize(g_validated_keys)+1);\n"
        "      g_validated_keys[ArraySize(g_validated_keys)-1] = key;\n"
        "      i++; // closing quote of the key",
    ),
    "JsonBoolExact ternary collapses true and false": (
        "(wanted ? 3 : 4)",
        "(wanted ? 3 : 3)",
    ),
    "parser boolean slot types swapped": (
        "StoreValidatedValue(key, token, c == 't' ? 3 : 4);",
        "StoreValidatedValue(key, token, c == 't' ? 4 : 3);",
    ),
    "kill-switch value check disabled with a parenthesised literal": (
        'if(!JsonFalse("kill_switch_active"))',
        'if(!JsonFalse("kill_switch_active") && (false))',
    ),
    "user approval forced with a parenthesised literal": (
        'bool user_approved = JsonTrue("user_approved");',
        'bool user_approved = JsonTrue("user_approved") || (true);',
    ),
    "approval flag reassigned after validation": (
        'bool user_approved = JsonTrue("user_approved");',
        'bool user_approved = JsonTrue("user_approved");\n   user_approved = true;',
    ),
    "identifier validation disabled with a parenthesised literal": (
        "if(!IsSafeIdentifier(command_id) || !IsSafeIdentifier(order_id) || !IsSafeIdentifier(symbol))",
        "if((!IsSafeIdentifier(command_id) || !IsSafeIdentifier(order_id) || !IsSafeIdentifier(symbol)) && (false))",
    ),
    "nested-value rejection keeps the message but stops returning": (
        """if(c == '{' || c == '[') { error = "nested value rejected"; return false; }""",
        """if(c == '{' || c == '[') { error = "nested value rejected"; }""",
    ),
    "escape rejection keeps the message but stops returning": (
        r"""if(vc == '\\') { error = "escape sequences rejected"; return false; }""",
        r"""if(vc == '\\') { error = "escape sequences rejected"; }""",
    ),
    "control-character rejection keeps the message but stops returning": (
        """if(kc < 0x20) { error = "control character rejected"; return false; }""",
        """if(kc < 0x20) { error = "control character rejected"; }""",
    ),
    "kill-switch value check keeps the message but stops returning": (
        '   {\n      Print("BLOCKED: kill switch is active or state is invalid.");\n      return false;\n   }\n',
        '   {\n      Print("BLOCKED: kill switch is active or state is invalid.");\n   }\n',
    ),
    "user approval gate keeps the message but stops returning": (
        'if(mode != "demo" || decision != "APPROVE" || !user_approved)\n'
        '   { Print("BLOCKED: demo mode and explicit user approval are required."); return false; }',
        'if(mode != "demo" || decision != "APPROVE" || !user_approved)\n'
        '   { Print("BLOCKED: demo mode and explicit user approval are required."); }',
    ),
    "identifier validation keeps the message but stops returning": (
        'if(!IsSafeIdentifier(command_id) || !IsSafeIdentifier(order_id) || !IsSafeIdentifier(symbol))\n'
        '   { Print("BLOCKED: command/order/symbol identifier contains unsupported characters."); return false; }',
        'if(!IsSafeIdentifier(command_id) || !IsSafeIdentifier(order_id) || !IsSafeIdentifier(symbol))\n'
        '   { Print("BLOCKED: command/order/symbol identifier contains unsupported characters."); }',
    ),
    "empty token accepted at init": (
        '   if(!IsValidBridgeToken(BridgeToken))\n'
        '   {\n'
        '      Print("BLOCKED: BridgeToken is empty or not 24..256 characters of [A-Za-z0-9._~-]; refusing to initialize.");',
        '   if(StringLen(BridgeToken) > 0 && !IsValidBridgeToken(BridgeToken))\n'
        '   {\n'
        '      Print("BLOCKED: BridgeToken is empty or not 24..256 characters of [A-Za-z0-9._~-]; refusing to initialize.");',
    ),
    # Round-7 counter-mutations: a validated value that is never bound, an inverted
    # one-line wrapper, a shadow endpoint, a shifted clock and unreachable guard copies.
    "protective stops detached from order request": (
        "   request.sl = NormalizeDouble(sl, digits);\n"
        "   request.tp = NormalizeDouble(tp, digits);\n",
        "",
    ),
    "JsonTrue wrapper inverted": (
        "bool JsonTrue(const string key) { return JsonBoolExact(key, true); }",
        "bool JsonTrue(const string key) { return JsonBoolExact(key, false); }",
    ),
    "JsonFalse wrapper inverted": (
        "bool JsonFalse(const string key) { return JsonBoolExact(key, false); }",
        "bool JsonFalse(const string key) { return JsonBoolExact(key, true); }",
    ),
    "web request redirected to a shadow URL input": (
        ('input string BridgeToken = "";',
         'input string BridgeToken = "";\n'
         'input string ShadowUrl = "http://10.1.2.3:9999/v1/execution/next";'),
        ('int code = WebRequest("GET", BridgeUrl, headers, 3000, data, result, result_headers);',
         'int code = WebRequest("GET", ShadowUrl, headers, 3000, data, result, result_headers);'),
    ),
    "expiry clock shifted backwards": (
        "double now_epoch = (double)TimeGMT();",
        "double now_epoch = (double)TimeGMT() - 315360000.0;",
    ),
    "approval flag reassigned on the same line": (
        '   bool user_approved = JsonTrue("user_approved");',
        '   bool user_approved = JsonTrue("user_approved"); user_approved = true;',
    ),
    "user approval guard copied to unreachable code": (
        ('if(mode != "demo" || decision != "APPROVE" || !user_approved)',
         'if(mode != "demo" || decision != "APPROVE")'),
        ('         " order_id=",order_id," order=",result.order," deal=",result.deal," retcode=",result.retcode);\n'
         '   return true;\n}',
         '         " order_id=",order_id," order=",result.order," deal=",result.deal," retcode=",result.retcode);\n'
         '   return true;\n'
         '   if(mode != "demo" || decision != "APPROVE" || !user_approved)\n'
         '   { Print("BLOCKED: demo mode and explicit user approval are required."); return false; }\n}'),
    ),
    # Round-8 counter-mutations: a chart input that can re-enable execution, a neutralised
    # sizing/stops guard, and validated locals that never reach the request.
    "demo execution enabled at compile time": (
        "const bool ENABLE_DEMO_EXECUTION = false;",
        "const bool ENABLE_DEMO_EXECUTION = true;",
    ),
    "volume/SL/TP guard disabled with a short-circuit literal": (
        "if(volume <= 0 || volume > MaxVolume || sl <= 0 || tp <= 0 || expected_price <= 0)",
        "if((volume <= 0 || volume > MaxVolume || sl <= 0 || tp <= 0 || expected_price <= 0) && 0)",
    ),
    "volume assignment detached from the request": (
        "   request.volume = volume;\n",
        "",
    ),
    "order type direction inverted": (
        'request.type = (direction=="BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;',
        'request.type = (direction=="BUY") ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;',
    ),
}

# Each mutation must be rejected for its intended reason, not just for any failure.
MUTATION_EXPECTATIONS: dict[str, str] = {
    "kill-switch recheck before send removed": "post-preflight kill-switch re-read",
    "kill-switch recheck commented out": "post-preflight kill-switch re-read",
    "token minimum lowered": "token minimum length is 24",
    "JsonString type guard removed": "JsonString enforces its value type",
    "JsonString re-parses the raw body": "JsonString does not re-parse the raw body",
    "symbol trade mode disabled": "symbol requires full trading mode",
    "tombstone flush removed": "tombstone flush precedes OrderSend",
    "tombstone verification removed": "tombstones are re-checked after persist",
    "tombstone verification short-circuited": "tombstones are re-checked after persist",
    "kill-switch URL parser removed": "OnInit validates both loopback URLs",
    "terminal trading check short-circuited": "terminal and account trading allowed check",
    "demo account comparison inverted": "demo account comparison direction",
    "user approval hard-coded": "explicit user approval required",
    "approval gate short-circuited": "approval gate includes user approval",
    "return filling fallback restored": "return filling fallback restored",
    "filling mode assumed": "FOK advertisement check",
    "duplicate tombstone check short-circuited": "duplicate order guard",
    "expiry whole-number guard removed": "expiry must be a whole positive number",
    "expiry grace introduced": "command expiry validation",
    "TTL bound widened": "command TTL upper bound comparison",
    "volume cap removed": "volume cap comparison",
    "slippage threshold widened": "slippage guard uses point value",
    "stops level check disabled": "broker stops level check",
    "volume step rounding removed": "volume step rounding",
    "duplicate key rejection removed": "duplicate JSON key guard",
    "duplicate key guard short-circuited": "duplicate JSON key guard",
    "loopback allowlist widened": "loopback allowlist widened with an extra host",
    "OrderCheck failure branch removed": "broker preflight failure branch",
    "direction validation removed": "direction validated before use",
    "identifier validation removed": "command identifier validated",
    "required key guard removed": "required command keys enforced",
    "retcode validation removed": "order result retcode validation",
    "kill switch accepts active state": "kill-switch response must explicitly be false",
    "kill-switch presence check removed": "kill-switch field must be present",
    "duplicate function definition injected": "CheckKillSwitch is defined exactly once",
    "blocked branch keeps the message but drops the return": "guarded block must return false: BLOCKED: slippage guard triggered.",
    "early success injected into the command path": "ExecuteDemoCommand has exactly one success return",
    "early acceptance injected into the parser": "ValidateFlatObject has exactly two success returns",
    "preprocessor macro injected": "preprocessor directive outside #property",
    "second order send injected": "exactly one broker OrderSend call",
    "dead parser loop": "dead loop",
    "FNV multiply constant weakened": "FNV multiply constant appears twice",
    "identifier alphabet widened": "identifier alphabet pinned",
    "token alphabet widened": "token alphabet pinned",
    "identifier length bound removed": "identifier length bounded",
    "URL leading-zero port guard removed": "URL port leading zero rejected",
    "URL port range widened": "URL port range checked",
    "required-key loop bound shrunk": "required key loop bounded by the array size",
    "boolean prefix accepted": "complete boolean literal required",
    "JsonNumber type guard removed": "JsonNumber enforces its value type",
    "write request introduced": "write request introduced",
    "max volume default widened": "volume cap default",
    "slippage default widened": "slippage default",
    "TTL default widened": "command TTL default",
    "kill-switch status check widened": "kill-switch HTTP failure is fail-closed",
    "URL minimum length removed": "URL minimum length",
    "URL printable check weakened": "URL printable ASCII only",
    "URL userinfo rejection removed": "URL userinfo separator rejected",
    "number leading-zero guard removed": "number token rejects a leading zero",
    "literal-key slot forged inside the parser": "ValidateFlatObject stores exactly three validated values",
    "parsed key replaced by a literal slot key": "validated slots are never stored under a literal key",
    "key slot appended by hand in the parser": "validated key slots are appended only by StoreValidatedValue",
    "JsonBoolExact ternary collapses true and false": "JsonBoolExact resolves the exact type slots",
    "parser boolean slot types swapped": "parser stores true as slot type 3 and false as slot type 4",
    "kill-switch value check disabled with a parenthesised literal": "literal short-circuit term is forbidden",
    "user approval forced with a parenthesised literal": "literal short-circuit term is forbidden",
    "approval flag reassigned after validation": "approval flag reassigned outside its declaration",
    "identifier validation disabled with a parenthesised literal": "literal short-circuit term is forbidden",
    "nested-value rejection keeps the message but stops returning": "parser rejects nested values in a complete fail-closed branch",
    "escape rejection keeps the message but stops returning": "parser rejects value escapes in a complete fail-closed branch",
    "control-character rejection keeps the message but stops returning": "parser rejects key control characters in a complete fail-closed branch",
    "kill-switch value check keeps the message but stops returning": "kill-switch value check is a complete fail-closed branch",
    "user approval gate keeps the message but stops returning": "user approval gate is a complete fail-closed branch",
    "identifier validation keeps the message but stops returning": "identifier validation is a complete fail-closed branch",
    "empty token accepted at init": "OnInit requires a configured bridge token before starting the timer",
    "protective stops detached from order request": "protective stop-loss is bound to the order request",
    "JsonTrue wrapper inverted": "JsonTrue wrapper calls JsonBoolExact with true",
    "JsonFalse wrapper inverted": "JsonFalse wrapper calls JsonBoolExact with false",
    "web request redirected to a shadow URL input": "command web request targets BridgeUrl",
    "expiry clock shifted backwards": "expiry clock origin is the terminal GMT time",
    "approval flag reassigned on the same line": "approval flag is assigned only in its single declaration",
    "user approval guard copied to unreachable code": "user approval gate is a complete fail-closed branch",
    "demo execution enabled at compile time": "demo execution is compile-time disabled",
    "volume/SL/TP guard disabled with a short-circuit literal": "literal short-circuit term is forbidden",
    "volume assignment detached from the request": "validated volume is bound to the order request",
    "order type direction inverted": "validated direction selects the order type",
}


def check_source(source: str) -> tuple[list[str], int]:
    """Return (failures, invariant_count) for the supplied EA source."""
    code = _strip_comments(source)
    failures = _required_failures(code) + _forbidden_failures(code)
    failures += _structural_problems(code)
    total = (len(_required_checks()) + len(_forbidden_checks())
             + len(_structural_checks(code)))
    return failures, total


def _is_json_number_token_model(text: str) -> bool:
    n = len(text)
    if n == 0:
        return False
    i = 0
    if text[0] == "-":
        i += 1
    int_start = i
    while i < n and "0" <= text[i] <= "9":
        i += 1
    if i <= int_start:
        return False
    if i - int_start > 1 and text[int_start] == "0":
        return False
    if i < n and text[i] == ".":
        i += 1
        frac_start = i
        while i < n and "0" <= text[i] <= "9":
            i += 1
        if i <= frac_start:
            return False
    if i < n and text[i] in "eE":
        i += 1
        if i < n and text[i] in "+-":
            i += 1
        exp_start = i
        while i < n and "0" <= text[i] <= "9":
            i += 1
        if i <= exp_start:
            return False
    return i == n


def validate_flat_object_model(text: str) -> tuple[bool, str]:
    """Python reference model of the EA's ValidateFlatObject grammar."""
    # The EA trims only spaces, tabs, CR and LF (StringTrimLeft/StringTrimRight), so a
    # leading/trailing NBSP or vertical tab must still be rejected here.
    s = text.strip(" \t\r\n")
    n = len(s)
    if n < 2 or s[0] != "{" or s[-1] != "}":
        return False, "body must be exactly one JSON object"

    def is_ws(c: str) -> bool:
        return c in " \t\r\n"

    keys: list[str] = []
    i = 1
    after_comma = False
    while True:
        while i < n and is_ws(s[i]):
            i += 1
        if i >= n:
            return False, "unterminated object"
        c = s[i]
        if c == "}":
            if i != n - 1:
                return False, "trailing content after object"
            if after_comma:
                return False, "trailing comma"
            return True, ""
        if c in "{[":
            return False, "nested object or array rejected"
        if c != '"':
            return False, "object key must be a string"
        i += 1
        key_start = i
        while i < n and s[i] != '"':
            kc = s[i]
            if kc == "\\":
                return False, "escape sequences rejected"
            if ord(kc) < 0x20:
                return False, "control character rejected"
            i += 1
        if i >= n:
            return False, "unterminated object key"
        key = s[key_start:i]
        if not key:
            return False, "empty object key"
        if key in keys:
            return False, "DUPLICATE JSON KEY: " + key
        keys.append(key)
        i += 1
        while i < n and is_ws(s[i]):
            i += 1
        if i >= n or s[i] != ":":
            return False, "missing ':' after key"
        i += 1
        while i < n and is_ws(s[i]):
            i += 1
        if i >= n:
            return False, "missing value"
        c = s[i]
        if c in "{[":
            return False, "nested value rejected"
        if c == '"':
            i += 1
            value_start = i
            while i < n and s[i] != '"':
                vc = s[i]
                if vc == "\\":
                    return False, "escape sequences rejected"
                if ord(vc) < 0x20:
                    return False, "control character rejected"
                i += 1
            if i >= n:
                return False, "unterminated string value"
            i += 1
        elif c in "tf":
            token = "true" if c == "t" else "false"
            tl = len(token)
            if i + tl >= n or s[i:i + tl] != token:
                return False, "invalid boolean literal"
            i += tl
        elif "0" <= c <= "9" or c == "-":
            start = i
            while i < n and s[i] not in ",}" and not is_ws(s[i]):
                i += 1
            token = s[start:i]
            try:
                finite = math.isfinite(float(token))
            except ValueError:
                finite = False
            if not _is_json_number_token_model(token) or not finite:
                return False, "invalid number literal"
        else:
            return False, "unsupported value type"
        while i < n and is_ws(s[i]):
            i += 1
        if i >= n:
            return False, "unterminated object"
        c = s[i]
        if c == ",":
            i += 1
            after_comma = True
            continue
        if c == "}":
            if i != n - 1:
                return False, "trailing content after object"
            return True, ""
        return False, "unexpected character after value"


def _load_cases() -> dict:
    if not FLAT_OBJECT_CASES.exists():
        return {}
    return json.loads(FLAT_OBJECT_CASES.read_text(encoding="utf-8"))


def model_cases() -> list[str]:
    """Prove the reference parser and the EA parser agree with the shared fixtures."""
    problems: list[str] = []
    if not FLAT_OBJECT_CASES.exists():
        return [f"shared fixture file missing: {FLAT_OBJECT_CASES}"]
    cases = _load_cases()
    for case in cases.get("accept", []):
        ok, error = validate_flat_object_model(case["json"])
        if not ok:
            problems.append(f"model rejected accept case '{case['name']}': {error}")
    for case in cases.get("reject", []):
        ok, error = validate_flat_object_model(case["json"])
        if ok:
            problems.append(f"model accepted reject case '{case['name']}'")
        elif case.get("reason", "") not in error:
            problems.append(f"model error mismatch for '{case['name']}': {error!r}")
    if len(cases.get("accept", [])) < 3 or len(cases.get("reject", [])) < 10:
        problems.append("fixture set is too small to exercise the parser")
    problems += _fixture_parser_parity(cases)
    return problems


# Error literals that the parser cannot reach because the single-brace-pair check
# rejects such bodies first. The required check "flat object requires exactly one brace
# pair" keeps that shadowing condition pinned, so the exemption cannot silently grow.
SHADOWED_PARSER_ERRORS = frozenset({"unterminated object", "missing value"})


def _fixture_parser_parity(cases: dict) -> list[str]:
    """Bind the fixtures to the EA parser in both directions."""
    if not EA.exists():
        return [f"EA source missing: {EA}"]
    parser = _function_body(_strip_comments(EA.read_text(encoding="utf-8")), "ValidateFlatObject") or ""
    if not parser:
        return ["ValidateFlatObject source is missing"]
    literals = set(re.findall(r'error\s*=\s*"([^"]*)"', parser))
    reasons = [case.get("reason", "") for case in cases.get("reject", [])]
    problems: list[str] = []
    for literal in sorted(literals):
        normalized = literal.rstrip(": ")
        if normalized in SHADOWED_PARSER_ERRORS:
            continue
        if not any(normalized in reason or reason == normalized for reason in reasons):
            problems.append(f"EA parser error literal {literal!r} has no matching fixture")
    for reason in sorted(set(reasons)):
        if reason and reason not in parser:
            problems.append(f"fixture reason {reason!r} is absent from the EA parser")
    return problems


def _mutation_edits(spec) -> tuple[tuple[str, str], ...]:
    """Normalise a catalogue entry to the list of (old, new) edits it performs.

    A single pair is one edit; a tuple of pairs models a counter-mutation that needs two
    coordinated changes (e.g. declare a shadow input and point the request at it).
    """
    if isinstance(spec[0], str):
        return (spec,)
    return tuple((old, new) for old, new in spec)


def self_test(source: str) -> list[str]:
    """Prove the checker rejects each known bypass for the expected reason."""
    problems: list[str] = []
    if set(MUTATIONS) != set(MUTATION_EXPECTATIONS):
        problems.append("mutation catalogue and expectations differ")
    for name, spec in MUTATIONS.items():
        edits = _mutation_edits(spec)
        if any(old not in source for old, _ in edits):
            problems.append(f"mutation anchor missing: {name}")
            continue
        mutated = source
        for old, new in edits:
            mutated = mutated.replace(old, new, 1)
        failures, _ = check_source(mutated)
        expected = MUTATION_EXPECTATIONS.get(name, "")
        if not failures:
            problems.append(f"checker accepted weakened source: {name}")
        elif expected and not any(expected in failure for failure in failures):
            problems.append(f"mutation {name!r} failed for an unexpected reason: {failures[:3]}")
    problems += model_cases()
    return problems


def main() -> int:
    source = EA.read_text(encoding="utf-8")
    failures, total = check_source(source)
    failures += self_test(source)
    if failures:
        for failure in failures:
            print("FAIL:", failure)
        return 1
    total += len(MUTATIONS)
    print(f"PASS: {total} static EA safety invariants verified.")
    print(f"LIMIT: {len(MUTATIONS)} mutations were rejected, but MQL5 compilation and "
          "terminal/demo-account execution are NOT PROVEN by static checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
