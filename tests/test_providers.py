import io
import json
from urllib.parse import urlparse

import pytest

from critical_consensus.providers import OpenAICompatibleAgent, canonical_signal_hash, load_agent_specs_from_env


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
    assert specs[0].agent_id == "a"\n    assert specs[0].role == "analyst"

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
