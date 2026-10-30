#!/usr/bin/env python3
"""Static safety guard checks for the EA source; this is not a MetaEditor compile test.

The checker is importable (``check_source``/``self_test``) so tests can prove that
weakening the EA is detected. Every invariant is a text-level guard: it catches
accidental regressions, not a determined rewrite, and it cannot prove compilation,
runtime behaviour, or broker-side execution.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EA = ROOT / "mql5" / "CriticalConsensusEA.mq5"


def _required_checks() -> dict[str, str]:
    return {
        "demo execution disabled by default": r"input\s+bool\s+EnableDemoExecution\s*=\s*false\s*;",
        "live execution switch disabled by default": r"input\s+bool\s+AllowLiveExecution\s*=\s*false\s*;",
        "demo account verification": r"ACCOUNT_TRADE_MODE_DEMO",
        "terminal trading allowed check": r"TerminalInfoInteger\s*\(\s*TERMINAL_TRADE_ALLOWED\s*\)",
        "account trading allowed check": r"AccountInfoInteger\s*\(\s*ACCOUNT_TRADE_ALLOWED\s*\)",
        "command expiry validation": r"expires_raw\s*<=\s*now_epoch",
        "expiry must be a whole positive number": r"MathFloor\s*\(\s*expires_raw\s*\)\s*!=\s*expires_raw",
        "command TTL upper bound comparison": r"expires_raw\s*>\s*now_epoch\s*\+\s*MaxCommandTtlSeconds",
        "protective stop-loss and take-profit validation": r"sl\s*<=\s*0\s*\|\|\s*tp\s*<=\s*0",
        "slippage guard": r"MaxSlippagePoints",
        "broker preflight": r"OrderCheck\s*\(",
        "localhost bridge default": r"http://127\.0\.0\.1:8000/v1/execution/next",
        "duplicate order guard": r"DUPLICATE BLOCKED",
        "kill-switch endpoint configured": r"KillSwitchUrl",
        "kill-switch response must explicitly be false": r"JsonFalse",
        "live-path remains blocked": r"if\s*\(!EnableDemoExecution\s*\|\|\s*AllowLiveExecution\)",
        "valid MQL5 OnInit signature": r"\bint\s+OnInit\s*\(\s*\)",
        "OnInit success return": r"return\s*\(\s*INIT_SUCCEEDED\s*\)",
        "OnInit failure return": r"return\s*\(\s*INIT_FAILED\s*\)",
        "strict flat-object JSON validation": r"bool\s+ValidateFlatObject\s*\(",
        "duplicate JSON key rejection": r"DUPLICATE JSON KEY",
        "idempotency persist failure is fail-closed": r"GlobalVariableSet\([^\n]*\)\s*==\s*0",
        "command TTL upper bound": r"MaxCommandTtlSeconds",
        "broker stops level check": r"SYMBOL_TRADE_STOPS_LEVEL",
        "explicit order filling mode": r"request\.type_filling",
        "direction validated before use": r'if\(direction != "BUY" && direction != "SELL"\)',
        "strict loopback URL parser": r"\bbool\s+IsLoopbackHttpUrl\s*\(",
        "bridge token format validation": r"\bbool\s+IsValidBridgeToken\s*\(",
        "bridge token charset is narrow": r"c == '\.' \|\| c == '_' \|\| c == '~' \|\| c == '-'",
        "URL userinfo separator rejected": r"c == '@' \|\| c == '#' \|\| c == '\?' \|\| c == '\\\\'",
        "idempotency key flushed to disk": r"\bGlobalVariablesFlush\s*\(\s*\)",
        # Validated-slot parser: lookups can never resolve to text inside a value.
        "validated key slots declared": r"string\s+g_validated_keys\[\]",
        "validated value slots declared": r"string\s+g_validated_values\[\]",
        "validated type slots declared": r"int\s+g_validated_types\[\]",
        "validated slot lookup helper": r"\bint\s+FindValidatedKey\s*\(",
        "validated slot store helper": r"\bvoid\s+StoreValidatedValue\s*\(",
        "strict RFC-8259 number token": r"\bbool\s+IsJsonNumberToken\s*\(",
        "number literal finiteness check": r"MathIsValidNumber\s*\(\s*StringToDouble\s*\(",
        "identifier safety check": r"\bbool\s+IsSafeIdentifier\s*\(",
        "command identifier validated": r"IsSafeIdentifier\s*\(\s*command_id\s*\)",
        "order identifier validated": r"IsSafeIdentifier\s*\(\s*order_id\s*\)",
        "symbol identifier validated": r"IsSafeIdentifier\s*\(\s*symbol\s*\)",
        # Tombstones bind both identifiers by hash instead of truncation.
        "command tombstone hashes command_id": r'return\s+"CC_TRIED_C_"\s*\+\s*Fnv1aHex\s*\(\s*command_id\s*\)',
        "order tombstone hashes client_order_id": r'return\s+"CC_TRIED_O_"\s*\+\s*Fnv1aHex\s*\(\s*order_id\s*\)',
        "full-string FNV-1a hash": r"string\s+Fnv1aHex\s*\(",
        # Round-4 execution hardening.
        "token minimum length is 24": r"if\s*\(\s*n\s*<\s*24\s*\|\|\s*n\s*>\s*256\s*\)\s*return\s+false\s*;",
        "symbol requires full trading mode": r"trade_mode\s*!=\s*SYMBOL_TRADE_MODE_FULL",
        "explicit user approval required": r'JsonTrue\s*\(\s*"user_approved"\s*\)',
        "approval gate includes user approval": r'decision\s*!=\s*"APPROVE"\s*\|\|\s*!user_approved',
        "kill-switch re-read before send": r"kill switch changed before send",
    }


def _forbidden_checks() -> dict[str, str]:
    return {
        "dead conditional branch": r"if\s*\(\s*false\s*[)&|]",
        "dead conditional branch (zero)": r"if\s*\(\s*0\s*\)",
        "condition disabled with && false": r"&&\s*false\s*\)",
        "condition forced with || true": r"\|\|\s*true\s*\)",
        "return filling fallback restored": r"ORDER_FILLING_RETURN",
        "truncating idempotency key restored": r"SanitizeKeyPart|IdempotencyKey",
        "void OnInit signature restored": r"^void\s+OnInit",
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


def _structural_checks(source: str) -> list[tuple[str, bool]]:
    checks: list[tuple[str, bool]] = []
    send = "OrderSend(request, result)"
    if send not in source:
        return [("expected broker send function is absent", False)]
    checks.append(("OrderCheck precedes OrderSend", _after(source, "OrderCheck(request, check)", send)))
    checks.append(("command tombstone precedes OrderSend", _after(source, "GlobalVariableSet(command_key", send)))
    checks.append(("order tombstone precedes OrderSend", _after(source, "GlobalVariableSet(order_key", send)))
    checks.append(("tombstone flush precedes OrderSend", _after(source, "GlobalVariablesFlush()", send)))
    checks.append(("duplicate key rejection is part of the parser",
                   _after(source, "DUPLICATE JSON KEY", "DUPLICATE BLOCKED")))

    parser = _function_body(source, "ValidateFlatObject")
    checks.append(("ValidateFlatObject function present", parser is not None))
    if parser is not None:
        for slot in ("g_validated_keys", "g_validated_values", "g_validated_types"):
            checks.append((f"ValidateFlatObject resets {slot}", f"ArrayResize({slot}, 0)" in parser))
        checks.append(("ValidateFlatObject stores all three scalar types",
                       parser.count("StoreValidatedValue(") >= 3))
        checks.append(("duplicate key rejection is inside ValidateFlatObject",
                       "DUPLICATE JSON KEY" in parser))
        checks.append(("number values use the strict token parser",
                       "IsJsonNumberToken(number_token)" in parser))

    lookups = {
        "JsonString": ("string", "return g_validated_values[index];", 1),
        "JsonNumber": ("double", "g_validated_values[index]", 2),
        "JsonBoolExact": ("bool", "g_validated_types[index] ==", None),
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

    command = _function_body(source, "ExecuteDemoCommand")
    checks.append(("ExecuteDemoCommand function present", command is not None))
    if command is not None:
        checks.append(("command body is validated before field extraction",
                       _after(command, "ValidateFlatObject(body", 'JsonString("command_id")')))
        checks.append(("direction is validated before it is used",
                       _after(command, 'direction != "BUY"', "live_price =")))
        checks.append(("kill switch is re-read after OrderCheck",
                       _after(command, "OrderCheck(request, check)", "CheckKillSwitch()")))
        checks.append(("kill switch re-read precedes the tombstone write",
                       _after(command, "CheckKillSwitch()", "GlobalVariableSet(command_key")))
        checks.append(("duplicate tombstone check precedes broker preflight",
                       _after(command, "GlobalVariableCheck(command_key)", "OrderCheck(request, check)")))

    timer = _function_body(source, "OnTimer", "void")
    checks.append(("OnTimer function present", timer is not None))
    if timer is not None:
        checks.append(("kill switch is checked before command polling",
                       _after(timer, "if(!CheckKillSwitch()) return;", "if(!FetchCommand(body)) return;")))

    kill = _function_body(source, "CheckKillSwitch")
    checks.append(("CheckKillSwitch function present", kill is not None))
    if kill is not None:
        checks.append(("kill-switch body is validated as a flat object first",
                       _after(kill, "ValidateFlatObject(response", "JsonFalse")))
        checks.append(("kill-switch token is validated before building headers",
                       _after(kill, "IsValidBridgeToken(BridgeToken)", 'headers += "X-Bridge-Key: "')))
        checks.append(("kill-switch response must contain the active flag",
                       'KeyInList("kill_switch_active")' in kill))

    fetch = _function_body(source, "FetchCommand")
    checks.append(("FetchCommand function present", fetch is not None))
    if fetch is not None:
        checks.append(("command token is validated before building headers",
                       _after(fetch, "IsValidBridgeToken(BridgeToken)", 'headers += "X-Bridge-Key: "')))

    oninit = _function_body(source, "OnInit", "int")
    checks.append(("OnInit function present", oninit is not None))
    if oninit is not None:
        checks.append(("OnInit validates both loopback URLs", oninit.count("IsLoopbackHttpUrl(") >= 2))
        checks.append(("OnInit fails closed", "return(INIT_FAILED)" in oninit))
        checks.append(("timer is started after endpoint validation",
                       _after(oninit, "IsLoopbackHttpUrl(KillSwitchUrl", "EventSetTimer")))
        checks.append(("OnInit rejects a malformed configured token",
                       _after(oninit, "IsValidBridgeToken(BridgeToken)", "EventSetTimer")))
    return checks


def _structural_problems(source: str) -> list[str]:
    return [name for name, ok in _structural_checks(source) if not ok]


MUTATIONS: dict[str, tuple[str, str]] = {
    "kill-switch recheck before send removed": (
        '   if(!CheckKillSwitch())\n   { Print("BLOCKED: kill switch changed before send."); return false; }\n',
        "",
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
    "tombstone flush removed": ("   GlobalVariablesFlush();\n", ""),
    "kill-switch URL parser removed": ("if(!IsLoopbackHttpUrl(KillSwitchUrl, url_error))", "if(false)"),
    "terminal trading check short-circuited": (
        "if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))",
        "if(false || !AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))",
    ),
    "user approval hard-coded": ('bool user_approved = JsonTrue("user_approved");', "bool user_approved = true;"),
    "approval gate short-circuited": ('if(mode != "demo" || decision != "APPROVE" || !user_approved)', "if(false)"),
    "return filling fallback restored": (
        'else { Print("BLOCKED: symbol advertises neither FOK nor IOC for a market deal."); return false; }',
        "else request.type_filling = ORDER_FILLING_RETURN;",
    ),
    "duplicate tombstone check short-circuited": (
        "if(GlobalVariableCheck(command_key) || GlobalVariableCheck(order_key))",
        "if(false)",
    ),
    "expiry whole-number guard removed": (
        "if(expires_raw <= 0 || MathFloor(expires_raw) != expires_raw)",
        "if(expires_raw <= 0)",
    ),
    "duplicate key rejection removed": (
        'error = "DUPLICATE JSON KEY: " + key;',
        "error = key;",
    ),
}


def check_source(source: str) -> tuple[list[str], int]:
    """Return (failures, invariant_count) for the supplied EA source."""
    failures = _required_failures(source) + _forbidden_failures(source)
    failures += _structural_problems(source)
    total = len(_required_checks()) + len(_forbidden_checks()) + len(_structural_checks(source))
    return failures, total


def self_test(source: str) -> list[str]:
    """Prove the checker rejects a weakened EA for each known bypass."""
    problems: list[str] = []
    for name, (old, new) in MUTATIONS.items():
        if old not in source:
            problems.append(f"mutation anchor missing: {name}")
            continue
        mutated = source.replace(old, new, 1)
        failures, _ = check_source(mutated)
        if not failures:
            problems.append(f"checker accepted weakened source: {name}")
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
