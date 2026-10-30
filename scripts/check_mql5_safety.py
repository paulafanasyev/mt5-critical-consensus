#!/usr/bin/env python3
"""Static safety guard checks for the EA source; this is not a MetaEditor compile test."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EA = ROOT / "mql5" / "CriticalConsensusEA.mq5"


def _pattern_checks(source: str) -> tuple[dict[str, str], list[str]]:
    required = {
        "demo execution disabled by default": r"input\s+bool\s+EnableDemoExecution\s*=\s*false\s*;",
        "live execution switch disabled by default": r"input\s+bool\s+AllowLiveExecution\s*=\s*false\s*;",
        "demo account verification": r"ACCOUNT_TRADE_MODE_DEMO",
        "command expiry validation": r"expires_at\s*<=\s*now_epoch",
        "protective stop-loss and take-profit validation": r"sl\s*<=\s*0\s*\|\|\s*tp\s*<=\s*0",
        "slippage guard": r"MaxSlippagePoints",
        "broker preflight": r"OrderCheck\s*\(",
        "localhost bridge default": r"http://127\.0\.0\.1:8000/v1/execution/next",
        "duplicate order guard": r"DUPLICATE BLOCKED",
        "kill-switch endpoint configured": r"KillSwitchUrl",
        "kill-switch response must explicitly be false": r"JsonFalse",
        "live-path remains blocked": r"if\s*\(!EnableDemoExecution\s*\|\|\s*AllowLiveExecution\)",
        # Added with the fail-closed hardening pass.
        "valid MQL5 OnInit signature": r"\bint\s+OnInit\s*\(\s*\)",
        "OnInit success return": r"return\s*\(\s*INIT_SUCCEEDED\s*\)",
        "OnInit failure return": r"return\s*\(\s*INIT_FAILED\s*\)",
        "strict flat-object JSON validation": r"bool\s+ValidateFlatObject\s*\(",
        "duplicate JSON key rejection": r"DUPLICATE JSON KEY",
        "idempotency persist failure is fail-closed": r"GlobalVariableSet\([^\n]*\)\s*==\s*0",
        "command TTL upper bound": r"MaxCommandTtlSeconds",
        "broker stops level check": r"SYMBOL_TRADE_STOPS_LEVEL",
        "explicit order filling mode": r"request\.type_filling",
        "idempotency key binds command_id and client_order_id": (
            r'"CC_TRIED_"\s*\+\s*SanitizeKeyPart\s*\(\s*command_id\s*\)\s*\+\s*"_"\s*\+'
            r"\s*SanitizeKeyPart\s*\(\s*order_id\s*\)"
        ),
        "direction validated before use": r'if\(direction != "BUY" && direction != "SELL"\)',
        # Added with the round-2 endpoint/token hardening pass.
        "strict loopback URL parser": r"\bbool\s+IsLoopbackHttpUrl\s*\(",
        "bridge token format validation": r"\bbool\s+IsValidBridgeToken\s*\(",
        "bridge token charset is narrow": r"c == '\.' \|\| c == '_' \|\| c == '~' \|\| c == '-'",
        "URL userinfo separator rejected": r"c == '@' \|\| c == '#' \|\| c == '\?' \|\| c == '\\\\'",
        "idempotency key flushed to disk": r"\bGlobalVariablesFlush\s*\(\s*\)",
        "idempotency key verified after flush": r"idempotency key is not durable after flush",
    }
    failures = [
        name
        for name, pattern in required.items()
        if re.search(pattern, source, re.M) is None
    ]
    return required, failures


def _after(haystack: str, first: str, second: str) -> bool:
    """True when both fragments exist and `first` appears before `second`."""
    return first in haystack and second in haystack and haystack.index(first) < haystack.index(second)


def main() -> int:
    source = EA.read_text(encoding="utf-8")
    required, failures = _pattern_checks(source)
    if failures:
        for failure in failures:
            print("FAIL:", failure)
        return 1

    send = "OrderSend(request, result)"
    structural: list[tuple[str, bool]] = [
        ("expected broker send function is absent", send in source),
        ("invalid void OnInit signature present", re.search(r"^void\s+OnInit", source, re.M) is None),
        ("OrderSend appears before OrderCheck", _after(source, "OrderCheck(request, check)", send)),
        ("idempotency key is not persisted before OrderSend",
         _after(source, "GlobalVariableSet(global_key", send)),
        ("idempotency key is not flushed before OrderSend",
         _after(source, "GlobalVariableSet(global_key", "GlobalVariablesFlush()")
         and _after(source, "GlobalVariablesFlush()", send)),
    ]

    timer = re.search(r"void\s+OnTimer\s*\(\s*\)\s*\{(?P<body>.*?)\n\}", source, re.S)
    if timer is None:
        print("FAIL: OnTimer function missing")
        return 1
    body = timer.group("body")
    structural.append(("kill switch is not checked before command polling",
                       _after(body, "if(!CheckKillSwitch()) return;", "if(!FetchCommand(body)) return;")))

    command = re.search(r"bool\s+ExecuteDemoCommand\s*\([^)]*\)\s*\{(?P<body>.*?)\n\}", source, re.S)
    if command is None:
        print("FAIL: ExecuteDemoCommand function missing")
        return 1
    command_body = command.group("body")
    structural.append(("command body is not validated before field extraction",
                       _after(command_body, "ValidateFlatObject(body", "JsonString(body")))
    structural.append(("direction is used before it is validated",
                       _after(command_body, 'direction != "BUY"', "live_price =")))
    structural.append(("duplicate key check precedes duplicate order guard",
                       _after(source, "DUPLICATE JSON KEY", "DUPLICATE BLOCKED")))

    kill = re.search(r"bool\s+CheckKillSwitch\s*\(\s*\)\s*\{(?P<body>.*?)\n\}", source, re.S)
    if kill is None:
        print("FAIL: CheckKillSwitch function missing")
        return 1
    kill_body = kill.group("body")
    structural.append(("kill-switch body is not validated as a flat object first",
                       _after(kill_body, "ValidateFlatObject(response", "JsonFalse")))
    structural.append(("kill-switch token is not validated before building headers",
                       _after(kill_body, "IsValidBridgeToken(BridgeToken)", 'headers += "X-Bridge-Key: "')))

    fetch = re.search(r"bool\s+FetchCommand\s*\([^)]*\)\s*\{(?P<body>.*?)\n\}", source, re.S)
    if fetch is None:
        print("FAIL: FetchCommand function missing")
        return 1
    fetch_body = fetch.group("body")
    structural.append(("command token is not validated before building headers",
                       _after(fetch_body, "IsValidBridgeToken(BridgeToken)", 'headers += "X-Bridge-Key: "')))

    oninit = re.search(r"\bint\s+OnInit\s*\(\s*\)\s*\{(?P<body>.*?)\n\}", source, re.S)
    if oninit is None:
        print("FAIL: OnInit function missing")
        return 1
    init_body = oninit.group("body")
    structural.append(("OnInit does not strictly validate both loopback URLs",
                       init_body.count("IsLoopbackHttpUrl(") >= 2
                       and "return(INIT_FAILED)" in init_body))
    structural.append(("timer is started before endpoint validation",
                       _after(init_body, "IsLoopbackHttpUrl(KillSwitchUrl", "EventSetTimer")))
    structural.append(("OnInit accepts a malformed configured token",
                       _after(init_body, "IsValidBridgeToken(BridgeToken)", "EventSetTimer")))

    for name, ok in structural:
        if not ok:
            print("FAIL:", name)
            return 1

    total = len(required) + len(structural)
    print(f"PASS: {total} static EA safety invariants verified.")
    print("LIMIT: MQL5 compilation and terminal/demo-account execution are NOT PROVEN by static checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
