import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ea_source() -> str:
    return (_root() / "mql5" / "CriticalConsensusEA.mq5").read_text(encoding="utf-8")


def _checker():
    path = _root() / "scripts" / "check_mql5_safety.py"
    spec = importlib.util.spec_from_file_location("check_mql5_safety", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mql5_static_safety_script_passes_but_does_not_claim_compile():
    root = _root()
    result = subprocess.run([sys.executable, str(root / "scripts" / "check_mql5_safety.py")],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "static EA safety invariants verified" in result.stdout
    assert "mutations were rejected" in result.stdout
    assert "NOT PROVEN" in result.stdout


def test_checker_finds_no_failure_and_enforces_a_broad_invariant_set():
    failures, total = _checker().check_source(_ea_source())
    assert failures == []
    assert total >= 100


def test_checker_rejects_every_known_weakening():
    checker = _checker()
    source = _ea_source()
    assert checker.self_test(source) == []
    # Independently prove a weakening that is not in the mutation catalogue is caught.
    weakened = source.replace("if(n < 24 || n > 256) return false;", "if(false) return false;", 1)
    failures, _ = checker.check_source(weakened)
    assert failures


def test_oninit_uses_int_signature_and_fails_closed_on_remote_urls():
    source = _ea_source()
    assert "int OnInit(" in source
    assert "void OnInit(" not in source
    assert "return(INIT_SUCCEEDED)" in source
    oninit = re.search(r"int\s+OnInit\s*\(\s*\)\s*\{(?P<body>.*?)\n\}", source, re.S)
    assert oninit is not None
    body = oninit.group("body")
    assert body.count("IsLoopbackHttpUrl(") >= 2
    assert "IsLoopbackHttpUrl(BridgeUrl" in body
    assert "IsLoopbackHttpUrl(KillSwitchUrl" in body
    assert "return(INIT_FAILED)" in body
    assert "WARNING: Bridge URL is not localhost" not in source
    assert body.index("IsLoopbackHttpUrl(KillSwitchUrl") < body.index("EventSetTimer")
    assert body.index("IsValidBridgeToken(BridgeToken)") < body.index("EventSetTimer")


def test_ea_rejects_userinfo_and_malformed_loopback_urls():
    source = _ea_source()
    assert re.search(r"\bbool\s+IsLoopbackHttpUrl\s*\(", source)
    for marker in ("c == '@'", "c == '#'", "c == '?'"):
        assert marker in source
    assert '"127.0.0.1" && host != "localhost"' in source
    assert "URL port must be numeric" in source
    assert "URL port is out of range" in source
    assert "StringFind(BridgeUrl, \"http://127.0.0.1:\") != 0" not in source


def test_ea_requires_header_safe_token_and_durable_idempotency_tombstone():
    source = _ea_source()
    assert re.search(r"\bbool\s+IsValidBridgeToken\s*\(", source)
    assert "if(n < 24 || n > 256) return false;" in source
    assert "IsValidBridgeToken(BridgeToken)" in source
    assert source.index("IsValidBridgeToken(BridgeToken)") < source.index('headers += "X-Bridge-Key: "')
    assert re.search(r"GlobalVariablesFlush\s*\(\s*\)", source)
    assert source.index("GlobalVariableSet(command_key") < source.index("GlobalVariablesFlush()")
    assert source.index("GlobalVariableSet(order_key") < source.index("GlobalVariablesFlush()")
    assert source.index("GlobalVariablesFlush()") < source.index("OrderSend(request, result)")


def test_every_schema_required_order_command_key_is_known_to_the_ea():
    required = json.loads(
        (_root() / "schemas" / "order_command.schema.json").read_text(encoding="utf-8")
    )["required"]
    assert required, "schema must declare required fields"
    source = _ea_source()
    missing = [key for key in required if f'"{key}"' not in source]
    assert missing == [], f"EA is unaware of required OrderCommand keys: {missing}"


def test_ea_validates_a_full_command_object_before_extracting_fields():
    source = _ea_source()
    assert "DUPLICATE JSON KEY" in source
    assert re.search(r"\bbool\s+ValidateFlatObject\s*\(", source)
    assert re.search(r"expires_raw\s*>\s*now_epoch\s*\+\s*MaxCommandTtlSeconds", source)
    assert re.search(r"input\s+double\s+MaxCommandTtlSeconds\s*=\s*600\s*;", source)
    command = re.search(r"bool\s+ExecuteDemoCommand\s*\([^)]*\)\s*\{(?P<body>.*?)\n\}", source, re.S)
    assert command is not None
    body = command.group("body")
    assert body.index("ValidateFlatObject(body") < body.index('JsonString("command_id")')
    assert body.index('direction != "BUY"') < body.index("live_price =")
    assert body.index("GlobalVariableCheck(command_key)") < body.index("OrderCheck(request, check)")
    assert body.index("OrderCheck(request, check)") < body.index("CheckKillSwitch()")
    assert body.index("CheckKillSwitch()") < body.index("GlobalVariableSet(command_key")


def test_ea_lookups_read_only_validated_parser_slots():
    source = _ea_source()
    assert "string g_validated_keys[];" in source
    assert "string g_validated_values[];" in source
    assert "int    g_validated_types[];" in source
    for name in ("JsonString", "JsonNumber", "JsonBoolExact"):
        body = re.search(rf"\b(?:string|double|bool)\s+{name}\s*\([^)]*\)\s*\{{(?P<body>.*?)\n\}}",
                         source, re.S)
        assert body is not None, name
        text = body.group("body")
        assert "FindValidatedKey(key)" in text
        assert "StringFind(" not in text and "StringSubstr(" not in text
    # Text inside a string value must never be readable as another field.
    assert 'return g_validated_values[index];' in source


def test_ea_binds_both_identifiers_by_hash_not_truncation():
    source = _ea_source()
    assert 'return "CC_TRIED_C_" + Fnv1aHex(command_id);' in source
    assert 'return "CC_TRIED_O_" + Fnv1aHex(order_id);' in source
    assert "SanitizeKeyPart" not in source
    assert "Fnv1aHex(command_id)" in source and "Fnv1aHex(order_id)" in source


def test_ea_blocks_live_and_unsafe_broker_states_before_send():
    source = _ea_source()
    assert "ACCOUNT_TRADE_MODE_DEMO" in source
    assert "TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)" in source
    assert "AccountInfoInteger(ACCOUNT_TRADE_ALLOWED)" in source
    assert "trade_mode != SYMBOL_TRADE_MODE_FULL" in source
    assert "ORDER_FILLING_RETURN" not in source
    assert 'JsonTrue("user_approved")' in source
    assert 'decision != "APPROVE" || !user_approved' in source
    assert "if(expires_raw <= 0 || MathFloor(expires_raw) != expires_raw)" in source
