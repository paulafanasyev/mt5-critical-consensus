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
        "kill-switch endpoint configured": r"KillSwitchUrl",
        "kill-switch response must explicitly be false": r"JsonFalse",
        "strict JSON boolean parser is present": r"JsonBooleanValue",
        "boolean token must end at a JSON delimiter": r"return\\s+tail==','\\s*\\|\\|\\s*tail=='}'",
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
    attempt = execute_body.find("GlobalVariableSet(global_key")
    send = execute_body.find("OrderSend(request, result)")
    if min(preflight, recheck, attempt, send) < 0 or not (preflight < recheck < attempt < send):
        print("FAIL: fresh kill-switch recheck must follow OrderCheck and precede the order attempt")
        return 1
    print(f"PASS: {len(required) + 5} static EA safety invariants verified.")
    print("LIMIT: MQL5 compilation and terminal/demo-account execution are NOT PROVEN by static checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
