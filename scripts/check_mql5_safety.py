#!/usr/bin/env python3
"""Static safety guard checks for the EA source; this is not a MetaEditor compile test."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EA = ROOT / "mql5" / "CriticalConsensusEA.mq5"


def main() -> int:
    source = EA.read_text(encoding="utf-8")
    required = {
        "demo execution disabled by default": r"input\s+bool\s+EnableDemoExecution\s*=\s*false\s*;",
        "live execution switch disabled by default": r"input\s+bool\s+AllowLiveExecution\s*=\s*false\s*;",
        "demo account verification": r"ACCOUNT_TRADE_MODE_DEMO",
        "command expiry validation": r"expires_at\s*<=\s*\(long\)TimeGMT\(\)",
        "protective stop-loss and take-profit validation": r"sl\s*<=\s*0\s*\|\|\s*tp\s*<=\s*0",
        "slippage guard": r"MaxSlippagePoints",
        "live spread cap": r"MaxSpreadPoints",
        "spread must be checked before order construction": r"\(tick\.ask\s*-\s*tick\.bid\)\s*/\s*point\s*>\s*MaxSpreadPoints",
        "broker preflight": r"OrderCheck\s*\(",
        "localhost bridge default": r"http://127\.0\.0\.1:8000/v1/execution/next",
        "duplicate order guard": r"DUPLICATE BLOCKED",
        "atomic account execution mutex": r"GlobalVariableSetOnCondition\s*\(",
        "temporary mutex creation": r"GlobalVariableTemp\s*\(",
        "persistent marker flushed before send": r"GlobalVariablesFlush\s*\(\)",
        "order key bound to account login": r"AccountInfoInteger\(ACCOUNT_LOGIN\)",
        "order key rejects values that could be truncated": r"StringLen\(order_id\)\s*>\s*38",
        "order key rejects ambiguous characters": r"if\s*\(!allowed\)\s*return\s*\"\";",
        "order key enforces terminal name limit": r"StringLen\(key\)\s*>\s*63",
        "persistent marker write failure is checked": r"marker_written\s*==\s*0",
        "idempotency expiry limitation documented": r"four weeks of inactivity",
        "server ledger requirement documented": r"PostgreSQL idempotency ledger",
        "legacy order-attempt markers survive EA upgrades": r"LegacyGlobalKey\\(order_id\\)",
        "legacy order-attempt marker checked": r"GlobalVariableCheck\\(legacy_global_key\\)",
        "kill-switch endpoint configured": r"KillSwitchUrl",
        "kill-switch response must explicitly be false": r"JsonFalse",
        "strict JSON boolean parser is present": r"JsonBooleanValue",
        "boolean token must end at a JSON delimiter": r"return\s+tail==','\s*\|\|\s*tail=='}'",
        "live-path remains blocked": r"if\s*\(!EnableDemoExecution\s*\|\|\s*AllowLiveExecution\)",
    }
    failures = [name for name, pattern in required.items() if re.search(pattern, source) is None]
    if failures:
        for failure in failures:
            print("FAIL:", failure)
        return 1
    if "OrderSend(request, result)" not in source:
        print("FAIL: expected broker send function is absent; source changed unexpectedly")
        return 1
    if source.index("OrderCheck(request, check)") > source.index("OrderSend(request, result)"):
        print("FAIL: OrderSend appears before OrderCheck")
        return 1
    timer = re.search(r"void\s+OnTimer\s*\(\s*\)\s*\{(?P<body>.*?)\n\}", source, re.S)
    if timer is None:
        print("FAIL: OnTimer function missing")
        return 1
    body = timer.group("body")
    if "if(!CheckKillSwitch()) return;" not in body:
        print("FAIL: kill switch is not checked before command polling")
        return 1
    if body.index("if(!CheckKillSwitch()) return;") > body.index("if(!FetchCommand(body)) return;"):
        print("FAIL: command polling appears before the kill-switch check")
        return 1

    execute_start = source.find("bool ExecuteDemoCommand")
    execute_end = source.find("\nvoid OnInit", execute_start)
    if execute_start < 0 or execute_end < 0:
        print("FAIL: ExecuteDemoCommand function boundaries are missing")
        return 1
    execute_body = source[execute_start:execute_end]
    preflight = execute_body.find("if(!OrderCheck(request, check))")
    recheck = execute_body.find("if(!CheckKillSwitch())")
    lock = execute_body.find("if(!AcquireExecutionLock(lock_key, lock_lease))")
    duplicate = execute_body.find("if(GlobalVariableCheck(global_key))")
    attempt = execute_body.find("GlobalVariableSet(global_key")
    flush = execute_body.find("GlobalVariablesFlush()")
    release = execute_body.find("ReleaseExecutionLock(lock_key, lock_lease)", flush)
    send = execute_body.find("OrderSend(request, result)")
    if min(preflight, recheck, lock, duplicate, attempt, flush, release, send) < 0:
        print("FAIL: one or more order attempt controls are missing")
        return 1
    if not (preflight < recheck < lock < duplicate < attempt < flush < release < send):
        print("FAIL: order attempt must be locked, duplicate-checked, flushed and unlocked before OrderSend")
        return 1
    print(f"PASS: {len(required) + 5} static EA safety invariants verified.")
    print("LIMIT: MQL5 compilation and terminal/demo-account execution are NOT PROVEN by static checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
