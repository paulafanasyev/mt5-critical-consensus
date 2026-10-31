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


def test_ea_refuses_an_empty_token_and_verifies_tombstones_before_sending():
    source = _ea_source()
    oninit = re.search(r"int\s+OnInit\s*\(\s*\)\s*\{(?P<body>.*?)\n\}", source, re.S).group("body")
    # An empty token must refuse initialization instead of polling and logging BLOCKED forever.
    assert "if(!IsValidBridgeToken(BridgeToken))" in oninit
    assert "StringLen(BridgeToken) > 0 &&" not in oninit
    assert oninit.index("if(!IsValidBridgeToken(BridgeToken))") < oninit.index("EventSetTimer")
    command = re.search(r"bool\s+ExecuteDemoCommand\s*\([^)]*\)\s*\{(?P<body>.*?)\n\}", source, re.S)
    assert command is not None
    body = command.group("body")
    # GlobalVariablesFlush() is void in build 6295, so the persisted keys themselves must be
    # re-read after both writes and before the send.
    verify = "if(!GlobalVariableCheck(command_key) || !GlobalVariableCheck(order_key))"
    assert verify in body
    assert re.search(r"(?m)^\s*GlobalVariablesFlush\(\);\s*$", body) is not None
    assert body.index(verify) < body.rindex("GlobalVariablesFlush()")
    assert body.index(verify) < body.index("OrderSend(request, result)")
    assert "!GlobalVariablesFlush" not in source
    # The unused expiry cast was removed; expiry is compared in double precision instead.
    assert "long expires_at =" not in source


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
                 "boolean prefix accepted",
                 "literal-key slot forged inside the parser",
                 "parsed key replaced by a literal slot key",
                 "JsonBoolExact ternary collapses true and false",
                 "parser boolean slot types swapped",
                 "kill-switch value check disabled with a parenthesised literal",
                 "user approval forced with a parenthesised literal",
                 "identifier validation disabled with a parenthesised literal",
                 "nested-value rejection keeps the message but stops returning",
                 "escape rejection keeps the message but stops returning",
                 "control-character rejection keeps the message but stops returning",
                 "kill-switch value check keeps the message but stops returning",
                 "user approval gate keeps the message but stops returning",
                 "identifier validation keeps the message but stops returning",
                 "empty token accepted at init",
                 "tombstone verification removed",
                 "tombstone verification short-circuited",
                 "protective stops detached from order request",
                 "JsonTrue wrapper inverted",
                 "JsonFalse wrapper inverted",
                 "web request redirected to a shadow URL input",
                 "expiry clock shifted backwards",
                 "approval flag reassigned on the same line",
                 "user approval guard copied to unreachable code"):
        assert name in checker.MUTATIONS, name


def test_checker_rejects_round7_counter_mutations_and_names_the_cause():
    """Each reviewed counter-mutation must be rejected for its own invariant."""
    checker = _checker()
    source = _ea_source()
    reviewed = (
        "protective stops detached from order request",
        "JsonTrue wrapper inverted",
        "JsonFalse wrapper inverted",
        "web request redirected to a shadow URL input",
        "expiry clock shifted backwards",
        "approval flag reassigned on the same line",
        "user approval guard copied to unreachable code",
    )
    for name in reviewed:
        spec = checker.MUTATIONS[name]
        mutated = source
        for old, new in checker._mutation_edits(spec):
            assert old in mutated, (name, old[:80])
            mutated = mutated.replace(old, new, 1)
        assert mutated != source, name
        failures, _ = checker.check_source(mutated)
        expected = checker.MUTATION_EXPECTATIONS[name]
        assert any(expected in failure for failure in failures), (name, failures[:5])


def test_checker_pins_stops_urls_clock_and_approval_tokens_independently():
    """Prove the round-7 invariants fail on their own, not only via the catalogue."""
    checker = _checker()
    source = _ea_source()
    weakenings = {
        # Validated locals that never reach the request.
        "sl detached": (
            "request.sl = NormalizeDouble(sl, digits);",
            "request.sl = 0;",
            "protective stop-loss is bound to the order request",
        ),
        "tp overwritten": (
            "   request.tp = NormalizeDouble(tp, digits);\n",
            "   request.tp = NormalizeDouble(tp, digits);\n   request.tp = 0.0;\n",
            "exactly one protective take-profit assignment to the request",
        ),
        # Wrapper polarity.
        "JsonTrue false": (
            "bool JsonTrue(const string key) { return JsonBoolExact(key, true); }",
            "bool JsonTrue(const string key) { return JsonBoolExact(key, (!true)); }",
            "JsonTrue is exactly one JsonBoolExact(key, true) call",
        ),
        "JsonFalse true": (
            "bool JsonFalse(const string key) { return JsonBoolExact(key, false); }",
            "bool JsonFalse(const string key) { return JsonBoolExact(key, true); }",
            "JsonFalse wrapper calls JsonBoolExact with false",
        ),
        # WebRequest target.
        "shadow local url": (
            'WebRequest("GET", BridgeUrl,',
            'WebRequest("GET", StringSubstr(KillSwitchUrl, 0, 10),',
            "command web request targets BridgeUrl",
        ),
        # Clock origin.
        "clock offset": (
            "double now_epoch = (double)TimeGMT();",
            "double now_epoch = (double)TimeGMT() + 86400.0;",
            "expiry clock origin is the terminal GMT time",
        ),
        # Any second assignment token, including one appended on the same line.
        "approval compound assignment": (
            'bool user_approved = JsonTrue("user_approved");',
            'bool user_approved = JsonTrue("user_approved"); user_approved |= true;',
            "approval flag is assigned only in its single declaration",
        ),
    }
    for label, (old, new, invariant) in weakenings.items():
        weakened = source.replace(old, new, 1)
        assert weakened != source, label
        failures, _ = checker.check_source(weakened)
        assert invariant in failures, (label, failures[:5])


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


def test_checker_rejects_forged_parser_slots_and_a_collapsed_boolean_slot():
    checker = _checker()
    source = _ea_source()
    # A fourth store, or a store keyed by a literal instead of the parsed key, forges a field
    # (user_approved / kill_switch_active) in every response.
    forged = source.replace(
        "StoreValidatedValue(key, token, c == 't' ? 3 : 4);",
        "StoreValidatedValue(key, token, c == 't' ? 3 : 4);\n"
        '         StoreValidatedValue("user_approved", "true", 3);',
        1,
    )
    assert forged != source
    failures, _ = checker.check_source(forged)
    assert "ValidateFlatObject stores exactly three validated values" in failures
    assert "validated slots are never stored under a literal key" in failures

    swapped = source.replace(
        "StoreValidatedValue(key, number_token, 2);",
        'StoreValidatedValue("kill_switch_active", "false", 4);',
        1,
    )
    assert swapped != source
    failures, _ = checker.check_source(swapped)
    assert "ValidateFlatObject keys every stored value by the parsed key" in failures

    # Collapsing the JsonBoolExact ternary makes literal true readable as literal false.
    collapsed = source.replace("(wanted ? 3 : 4)", "(wanted ? 3 : 3)", 1)
    assert collapsed != source
    failures, _ = checker.check_source(collapsed)
    assert "JsonBoolExact resolves the exact type slots" in failures

    # Swapping the slots the parser writes has the same effect from the other direction.
    swapped_types = source.replace(
        "StoreValidatedValue(key, token, c == 't' ? 3 : 4);",
        "StoreValidatedValue(key, token, c == 't' ? 4 : 3);",
        1,
    )
    assert swapped_types != source
    failures, _ = checker.check_source(swapped_types)
    assert "parser stores true as slot type 3 and false as slot type 4" in failures


def test_checker_rejects_parenthesised_boolean_literal_terms():
    checker = _checker()
    source = _ea_source()
    bypasses = {
        "kill-switch value": (
            'if(!JsonFalse("kill_switch_active"))',
            'if(!JsonFalse("kill_switch_active") && (false))',
        ),
        "user approval": (
            'bool user_approved = JsonTrue("user_approved");',
            'bool user_approved = JsonTrue("user_approved") || (true);',
        ),
        "identifier validation": (
            "if(!IsSafeIdentifier(command_id) || !IsSafeIdentifier(order_id) || !IsSafeIdentifier(symbol))",
            "if((!IsSafeIdentifier(command_id) || !IsSafeIdentifier(order_id) || "
            "!IsSafeIdentifier(symbol)) && ( false ))",
        ),
        "numeric guard": (
            "if(volume <= 0 || volume > MaxVolume || sl <= 0 || tp <= 0 || expected_price <= 0)",
            "if((volume <= 0 || volume > MaxVolume || sl <= 0 || tp <= 0 || expected_price <= 0) && 0)",
        ),
    }
    for label, (old, new) in bypasses.items():
        weakened = source.replace(old, new, 1)
        assert weakened != source, label
        stripped = checker._strip_comments(weakened)
        assert checker._parenthesised_literal_terms(stripped), label
        failures, _ = checker.check_source(weakened)
        assert "literal short-circuit term is forbidden" in failures, label
    # The parser's `while(true)` is a whole control condition, not a bypass term.
    assert checker._parenthesised_literal_terms(checker._strip_comments(source)) == []


def test_checker_requires_complete_guard_blocks_not_just_their_text():
    checker = _checker()
    source = _ea_source()
    weakenings = {
        "parser nested values": (
            """if(c == '{' || c == '[') { error = "nested value rejected"; return false; }""",
            """if(c == '{' || c == '[') { error = "nested value rejected"; }""",
            "parser rejects nested values in a complete fail-closed branch",
        ),
        "parser value escapes": (
            r"""if(vc == '\\') { error = "escape sequences rejected"; return false; }""",
            r"""if(vc == '\\') { error = "escape sequences rejected"; }""",
            "parser rejects value escapes in a complete fail-closed branch",
        ),
        "parser key control characters": (
            """if(kc < 0x20) { error = "control character rejected"; return false; }""",
            """if(kc < 0x20) { error = "control character rejected"; }""",
            "parser rejects key control characters in a complete fail-closed branch",
        ),
        "kill-switch value": (
            '   {\n      Print("BLOCKED: kill switch is active or state is invalid.");\n'
            "      return false;\n   }\n",
            '   {\n      Print("BLOCKED: kill switch is active or state is invalid.");\n   }\n',
            "kill-switch value check is a complete fail-closed branch",
        ),
        "user approval": (
            'if(mode != "demo" || decision != "APPROVE" || !user_approved)\n'
            '   { Print("BLOCKED: demo mode and explicit user approval are required."); return false; }',
            'if(mode != "demo" || decision != "APPROVE" || !user_approved)\n'
            '   { Print("BLOCKED: demo mode and explicit user approval are required."); }',
            "user approval gate is a complete fail-closed branch",
        ),
        "identifier validation": (
            "if(!IsSafeIdentifier(command_id) || !IsSafeIdentifier(order_id) || !IsSafeIdentifier(symbol))\n"
            '   { Print("BLOCKED: command/order/symbol identifier contains unsupported characters."); return false; }',
            "if(!IsSafeIdentifier(command_id) || !IsSafeIdentifier(order_id) || !IsSafeIdentifier(symbol))\n"
            '   { Print("BLOCKED: command/order/symbol identifier contains unsupported characters."); }',
            "identifier validation is a complete fail-closed branch",
        ),
    }
    for label, (old, new, invariant) in weakenings.items():
        weakened = source.replace(old, new, 1)
        assert weakened != source, label
        failures, _ = checker.check_source(weakened)
        assert invariant in failures, (label, failures)


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

