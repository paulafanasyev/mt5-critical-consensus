import json
import re
import subprocess
import sys
from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ea_source() -> str:
    return (_root() / "mql5" / "CriticalConsensusEA.mq5").read_text(encoding="utf-8")


def test_mql5_static_safety_script_passes_but_does_not_claim_compile():
    root = _root()
    result = subprocess.run([sys.executable, str(root / "scripts" / "check_mql5_safety.py")],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "static EA safety invariants verified" in result.stdout
    assert "NOT PROVEN" in result.stdout


def test_oninit_uses_int_signature_and_fails_closed_on_remote_urls():
    source = _ea_source()
    assert "int OnInit(" in source
    assert "void OnInit(" not in source
    assert "return(INIT_SUCCEEDED)" in source
    oninit = re.search(r"int\s+OnInit\s*\(\s*\)\s*\{(?P<body>.*?)\n\}", source, re.S)
    assert oninit is not None
    body = oninit.group("body")
    # Both endpoints must go through the strict parser and abort initialization on rejection.
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
    # The parser must reject every character that can rewrite the authority or query.
    for marker in ("c == '@'", "c == '#'", "c == '?'"):
        assert marker in source
    assert '"127.0.0.1" && host != "localhost"' in source
    assert "URL port must be numeric" in source
    assert "URL port is out of range" in source
    assert "StringFind(BridgeUrl, \"http://127.0.0.1:\") != 0" not in source


def test_ea_requires_header_safe_token_and_durable_idempotency_tombstone():
    source = _ea_source()
    assert re.search(r"\bbool\s+IsValidBridgeToken\s*\(", source)
    assert "IsValidBridgeToken(BridgeToken)" in source
    # Token must be validated before it is concatenated into a request header.
    assert source.index("IsValidBridgeToken(BridgeToken)") < source.index('headers += "X-Bridge-Key: "')
    assert re.search(r"GlobalVariablesFlush\s*\(\s*\)", source)
    assert source.index("GlobalVariableSet(global_key") < source.index("GlobalVariablesFlush()")
    assert source.index("GlobalVariablesFlush()") < source.index("OrderSend(request, result)")


def test_every_schema_required_order_command_key_is_known_to_the_ea():
    required = json.loads(
        (_root() / "schemas" / "order_command.schema.json").read_text(encoding="utf-8")
    )["required"]
    assert required, "schema must declare required fields"
    source = _ea_source()
    missing = [key for key in required if f'"{key}"' not in source]
    assert missing == [], f"EA is unaware of required OrderCommand keys: {missing}"


def test_ea_rejects_duplicate_json_keys_and_bounds_command_ttl():
    source = _ea_source()
    assert "DUPLICATE JSON KEY" in source
    assert re.search(r"\bbool\s+ValidateFlatObject\s*\(", source)
    assert re.search(r"expires_at\s*>\s*now_epoch\s*\+\s*\(long\)MaxCommandTtlSeconds", source)
    assert re.search(r"input\s+double\s+MaxCommandTtlSeconds\s*=\s*600\s*;", source)
    # The idempotency key must bind both identifiers and be checked before the send.
    assert re.search(r'"CC_TRIED_"\s*\+\s*SanitizeKeyPart\(command_id\)', source)
    assert source.index("GlobalVariableSet(global_key") < source.index("OrderSend(request, result)")
