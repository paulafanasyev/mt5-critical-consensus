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
    match = re.search(r"string\s+required_keys\[\]\s*=\s*\{(?P<body>[^}]*)\}", source)
    assert match is not None
    assert re.findall(r'"([^"]+)"', match.group("body")) == required


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


def test_ea_pins_exact_limits_for_size_slippage_ttl_and_filling():
    source = _ea_source()
    assert "AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO" in source
    assert "volume <= 0 || volume > MaxVolume" in source
    assert "MathAbs(live_price - expected_price) > MaxSlippagePoints * point" in source
    assert "expires_raw > now_epoch + MaxCommandTtlSeconds" in source
    assert "if(expires_raw <= now_epoch)" in source
    assert "if((filling & (long)SYMBOL_FILLING_FOK) != 0)       request.type_filling = ORDER_FILLING_FOK;" in source
    assert "else if((filling & (long)SYMBOL_FILLING_IOC) != 0)  request.type_filling = ORDER_FILLING_IOC;" in source
    assert "MathAbs(live_price - sl) < stops * point || MathAbs(tp - live_price) < stops * point" in source
    assert "if(KeyInList(key))" in source
    assert 'if(host != "127.0.0.1" && host != "localhost")' in source


def test_checker_strips_comments_so_a_commented_guard_cannot_pass():
    source = _ea_source()
    spoofed = source.replace(
        '   if(!OrderCheck(request, check))\n'
        '   { Print("BLOCKED: OrderCheck failed; retcode=",check.retcode," ",check.comment); return false; }\n',
        '   // if(!OrderCheck(request, check))\n'
        '   // { Print("BLOCKED: OrderCheck failed; retcode=",check.retcode," ",check.comment); return false; }\n'
        "   OrderCheck(request, check);\n",
        1,
    )
    assert spoofed != source
    failures, _ = _checker().check_source(spoofed)
    assert "broker preflight failure branch" in failures
    assert "broker preflight result ignored" in failures


def test_checker_rejects_each_pinned_predicate_inversion_independently():
    checker = _checker()
    source = _ea_source()
    inversions = {
        "demo": ("ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO", "ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_DEMO"),
        "volume": ("volume <= 0 || volume > MaxVolume", "volume <= 0"),
        "slippage": ("MaxSlippagePoints * point", "MaxSlippagePoints * 50 * point"),
        "ttl": ("now_epoch + MaxCommandTtlSeconds", "now_epoch + MaxCommandTtlSeconds * 1000"),
        "expiry": ("if(expires_raw <= now_epoch)", "if(expires_raw <= now_epoch - 86400)"),
        "stops": ("MathAbs(live_price - sl) < stops * point", "false"),
    }
    for label, (old, new) in inversions.items():
        weakened = source.replace(old, new, 1)
        assert weakened != source, label
        failures, _ = checker.check_source(weakened)
        assert failures, f"checker accepted the {label} weakening"


def _canonical_command() -> dict:
    return {
        "command_id": "cmd-1",
        "client_order_id": "ord-1",
        "permission_id": "perm-1",
        "signal_id": "sig-1",
        "instrument": "EURUSD",
        "direction": "BUY",
        "size": 0.10,
        "expected_price": 1.10000,
        "sl": 1.09900,
        "tp": 1.10200,
        "mode": "demo",
        "user_decision_id": "dec-1",
        "user_decision": "APPROVE",
        "user_approved": True,
        "expires_at_epoch": 1767225600,
        "payload_hash": "sha256:0f1e2d",
        "created_at": "2026-10-30T12:00:00Z",
    }


def test_reference_flat_object_model_accepts_bridge_encoded_commands():
    checker = _checker()
    command = _canonical_command()
    assert set(command) == set(json.loads(
        (_root() / "schemas" / "order_command.schema.json").read_text(encoding="utf-8")
    )["required"])
    for payload in (json.dumps(command, separators=(",", ":")),
                    json.dumps(command, sort_keys=True, indent=2),
                    json.dumps({**command, "auth_signature_ref": "sig-ref"})):
        ok, error = checker.validate_flat_object_model(payload)
        assert ok, (error, payload)


def test_reference_flat_object_model_matches_shared_fixtures():
    checker = _checker()
    cases = json.loads(
        (_root() / "config" / "mql5_flat_object_cases.json").read_text(encoding="utf-8")
    )
    names = {case["name"] for case in cases["reject"]}
    for required in ("duplicate key", "escaped quote in value", "control character in value",
                     "nested object value", "array value", "trailing comma", "leading zero"):
        assert required in names
    for case in cases["accept"]:
        ok, error = checker.validate_flat_object_model(case["json"])
        assert ok, (case["name"], error)
    for case in cases["reject"]:
        ok, error = checker.validate_flat_object_model(case["json"])
        assert not ok, case["name"]
        assert case["reason"] in error, (case["name"], error)
    assert checker.model_cases() == []


def test_production_checker_and_self_test_cover_the_new_predicates():
    checker = _checker()
    assert checker.check_source(_ea_source())[0] == []
    assert checker.self_test(_ea_source()) == []
    # Every mutation must be bound to the exact invariant expected to catch it.
    assert set(checker.MUTATIONS) == set(checker.MUTATION_EXPECTATIONS)
    for name in ("blocked branch keeps the message but drops the return",
                 "early success injected into the command path",
                 "early acceptance injected into the parser",
                 "preprocessor macro injected",
                 "second order send injected",
                 "dead parser loop",
                 "FNV multiply constant weakened",
                 "identifier alphabet widened",
                 "token alphabet widened",
                 "URL leading-zero port guard removed",
                 "required-key loop bound shrunk",
                 "boolean prefix accepted"):
        assert name in checker.MUTATIONS, name


def test_checker_rejects_guard_body_and_right_extension_bypasses_outside_the_catalogue():
    checker = _checker()
    source = _ea_source()
    bypasses = {
        # Keep the message, drop the return: the guard would only log.
        "guard return": (
            '{ Print("BLOCKED: slippage guard triggered."); return false; }',
            '{ Print("BLOCKED: slippage guard triggered."); }',
        ),
        # Extend a pinned threshold past its closing parenthesis.
        "slippage extension": (
            "MaxSlippagePoints * point)",
            "MaxSlippagePoints * point * 50)",
        ),
        "volume extension": ("volume > MaxVolume ||", "volume > MaxVolume * 1000 ||"),
        "ttl extension": ("now_epoch + MaxCommandTtlSeconds)", "now_epoch + MaxCommandTtlSeconds * 1000)"),
        "stops extension": (
            "< stops * point || MathAbs(tp - live_price) < stops * point)",
            "< stops * point * 10 || MathAbs(tp - live_price) < stops * point)",
        ),
        # Prepend a success return so the whole function becomes a no-op.
        "prepended success": (
            "bool CheckKillSwitch()\n{",
            "bool CheckKillSwitch()\n{ return true;",
        ),
        # A macro could redefine a limit the checks compare against.
        "macro definition": ("#property strict", "#property strict\n#define MaxVolume 999.0"),
    }
    for label, (old, new) in bypasses.items():
        weakened = source.replace(old, new, 1)
        assert weakened != source, label
        failures, _ = checker.check_source(weakened)
        assert failures, f"checker accepted the {label} bypass"


def test_parser_fixtures_are_bound_to_the_ea_parser_in_both_directions():
    checker = _checker()
    cases = json.loads(
        (_root() / "config" / "mql5_flat_object_cases.json").read_text(encoding="utf-8")
    )
    assert checker._fixture_parser_parity(cases) == []
    parser = re.search(
        r"bool\s+ValidateFlatObject\s*\([^)]*\)\s*\{(?P<body>.*?)\n\}", _ea_source(), re.S
    ).group("body")
    # Every fixture reason must be a literal the EA parser can actually produce.
    for case in cases["reject"]:
        assert case["reason"] in parser, case["name"]
    # Every reachable parser error literal must be exercised by a fixture.
    reasons = {case["reason"] for case in cases["reject"]}
    for literal in set(re.findall(r'error\s*=\s*"([^"]*)"', parser)):
        normalized = literal.rstrip(": ")
        if normalized in checker.SHADOWED_PARSER_ERRORS:
            continue
        assert any(normalized in reason for reason in reasons), literal
    # The shadowing exemption is only valid while the brace-pair check stays pinned.
    assert "StringGetCharacter(s,0) != '{'" in parser
    assert "StringGetCharacter(s,n-1) != '}'" in parser


def test_reference_model_trims_only_ascii_whitespace_like_the_ea():
    checker = _checker()
    for payload in ("\u00a0{}", "{}\u00a0", "\u000b{}", "{}\u000b"):
        ok, error = checker.validate_flat_object_model(payload)
        assert not ok, repr(payload)
        assert error == "body must be exactly one JSON object"

