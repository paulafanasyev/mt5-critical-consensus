import io
import json
from urllib.parse import urlparse

import pytest

from critical_consensus.providers import OpenAICompatibleAgent, canonical_signal_hash, load_agent_specs_from_env


def _extract_json_payload(prompt):
    start = prompt.find("{")
    if start < 0:
        raise AssertionError("provider prompt contains no JSON payload")
    value, _end = json.JSONDecoder().raw_decode(prompt[start:])
    return value


def test_openai_compatible_agent_binds_local_identity_and_rejects_unavailable_evidence(monkeypatch):
    signal = {"signal_id": "s1", "available_evidence_refs": ["market-bars:hash"], "market_snapshot": {"symbol": "EURUSD"}}
    content = json.dumps({"verdict": "ABSTAIN", "confidence": 0.4,
                          "evidence_refs": ["market-bars:hash"], "abstain_reason": "no news evidence",
                          "rationale": "market bars only", "risks": ["missing macro sources"]})
    envelope = {"choices": [{"message": {"content": content}}]}
    response = io.BytesIO(json.dumps(envelope).encode())

    class FakeOpener:
        def open(self, request, timeout):
            assert request.get_header("Authorization") == "Bearer test-secret"
            assert timeout == 2.0
            assert urlparse(request.full_url).scheme == "https"
            return response

    monkeypatch.setattr("critical_consensus.providers.urllib.request.build_opener",
                        lambda *handlers: FakeOpener())
    agent = OpenAICompatibleAgent(agent_id="technical", model="test-model", api_key="test-secret",
                                  timeout_seconds=2.0)
    import asyncio
    result = asyncio.run(agent(signal))
    assert result["agent_id"] == "technical"
    assert result["signal_id"] == "s1"
    assert result["input_hash"] == canonical_signal_hash(signal)
    assert result["verdict"] == "ABSTAIN"

    bad_envelope = {"choices": [{"message": {"content": json.dumps({
        "verdict": "APPROVE", "confidence": 0.9, "evidence_refs": ["made-up-source"],
        "abstain_reason": "", "rationale": "fabricated citation", "risks": [],
    })}}]}
    monkeypatch.setattr("critical_consensus.providers.urllib.request.build_opener",
                        lambda *handlers: type("FakeOpener", (), {
                            "open": lambda self, request, timeout: io.BytesIO(json.dumps(bad_envelope).encode())
                        })())
    with pytest.raises(RuntimeError, match="evidence_invalid"):
        asyncio.run(agent(signal))


def test_provider_config_requires_key_from_environment_not_inline(monkeypatch):
    monkeypatch.setenv("MT5_AGENT_PROVIDERS", json.dumps([{
        "agent_id": "a", "independence_group": "provider-a", "model": "model-a",
        "api_key_env": "A_API_KEY",
    }]))
    monkeypatch.setenv("A_API_KEY", "secret-for-test")
    specs = load_agent_specs_from_env()
    assert len(specs) == 1
    assert specs[0].agent_id == "a"
    assert specs[0].role == "analyst"
    assert specs[0].provider_origin == "api.openai.com"
    assert specs[0].provider_identity == "api.openai.com::model-a"

    monkeypatch.setenv("MT5_AGENT_PROVIDERS", json.dumps([{
        "agent_id": "a", "independence_group": "g", "model": "m", "api_key": "inline-secret",
    }]))
    with pytest.raises(ValueError, match="not inline JSON"):
        load_agent_specs_from_env()


def test_provider_redirect_handler_refuses_redirects():
    from critical_consensus.providers import _NoRedirectHandler
    assert _NoRedirectHandler().redirect_request(
        None, None, 302, "Found", {}, "https://attacker.example/collect"
    ) is None



@pytest.mark.parametrize("base_url", [
    "https://user:secret@provider.example/v1",
    "https://provider.example/v1?token=secret",
    "https://provider.example/v1#fragment",
    "https://127.0.0.1/v1",
    "http://provider.example/v1",
    "https://provider.example:8443/v1",
])
def test_provider_endpoint_rejects_userinfo_query_fragment_ip_and_nonstandard_ports(base_url):
    with pytest.raises(ValueError):
        OpenAICompatibleAgent(
            agent_id="agent", model="test-model", api_key="test-secret", base_url=base_url,
        )


def test_provider_origin_normalizes_hostname_case_and_trailing_dot():
    agent = OpenAICompatibleAgent(
        agent_id="agent", model="test-model", api_key="test-secret",
        base_url="https://Provider.Example./v1",
    )
    assert agent.provider_origin == "provider.example"


def test_peer_reviewer_receives_only_cited_source_documents(monkeypatch):
    agent = OpenAICompatibleAgent(
        agent_id="reviewer", model="test-model", api_key="test-secret",
    )
    captured = {}
    def fake_request_json_sync(*, system_prompt, user_prompt):
        captured["prompt"] = _extract_json_payload(user_prompt)
        return {"stance": "CONFIRM", "challenge_type": "NONE", "argument": "No contradiction found",
                "evidence_refs": ["source-a"]}
    monkeypatch.setattr(agent, "_request_json_sync", fake_request_json_sync)
    callback = agent.make_peer_reviewer(
        ["source-a", "source-b"],
        evidence_context={"source-a": "relevant evidence", "source-b": "unrelated private evidence"},
    )
    import asyncio
    callback_result = asyncio.run(callback(
        {"agent_id": "target", "evidence_refs": ["source-a"]},
        [{"agent_id": "peer", "evidence_refs": ["source-a"]}],
    ))
    assert callback_result["evidence_refs"] == ["source-a"]
    assert captured["prompt"]["allowed_evidence_refs"] == ["source-a"]
    assert captured["prompt"]["untrusted_source_documents"] == [
        {"source_id": "source-a", "text": "relevant evidence"}
    ]


def test_challenge_resolver_receives_only_challenge_cited_documents(monkeypatch):
    agent = OpenAICompatibleAgent(
        agent_id="resolver", model="test-model", api_key="test-secret",
    )
    captured = {}
    def fake_request_json_sync(*, system_prompt, user_prompt):
        captured["prompt"] = _extract_json_payload(user_prompt)
        return {"resolved": False, "summary": "insufficient", "evidence_refs": []}
    monkeypatch.setattr(agent, "_request_json_sync", fake_request_json_sync)
    resolver = agent.make_challenge_resolver(
        ["source-a", "source-b"],
        evidence_context={"source-a": "challenge evidence", "source-b": "unrelated evidence"},
    )
    result = resolver({"evidence_refs": ["source-b"]}, 1)
    assert result["resolved"] is False
    assert captured["prompt"]["allowed_evidence_refs"] == ["source-b"]
    assert captured["prompt"]["untrusted_source_documents"] == [
        {"source_id": "source-b", "text": "unrelated evidence"}
    ]


@pytest.mark.asyncio
async def test_provider_call_timeout_does_not_wait_for_blocking_thread_shutdown(monkeypatch):
    import asyncio
    import time

    agent = OpenAICompatibleAgent(
        agent_id="slow-agent", model="test-model", api_key="test-secret",
    )

    def slow_sync_call(_signal):
        time.sleep(0.3)
        return {"verdict": "ABSTAIN"}

    monkeypatch.setattr(agent, "_call_sync", slow_sync_call)
    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(agent({"signal_id": "timeout"}), timeout=0.02)
    elapsed = time.monotonic() - started
    assert elapsed < 0.15
    assert _provider_slots_available_for_test() is True
    time.sleep(0.32)


def _provider_slots_available_for_test():
    from critical_consensus.providers import _PROVIDER_CALL_SLOTS
    acquired = _PROVIDER_CALL_SLOTS.acquire(blocking=False)
    if acquired:
        _PROVIDER_CALL_SLOTS.release()
    return acquired
