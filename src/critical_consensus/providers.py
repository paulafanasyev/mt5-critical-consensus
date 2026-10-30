"""Configurable OpenAI-compatible agents; nothing calls a provider until explicitly invoked."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


def canonical_signal_hash(signal: Mapping[str, Any]) -> str:
    encoded = json.dumps(signal, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False, default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class OpenAICompatibleAgent:
    """Async adapter for a provider exposing POST /chat/completions.

    The provider returns its analysis only. This adapter assigns the trusted agent and
    signal IDs, hashes the exact input snapshot and supplies creation time/TTL locally.
    Evidence references are constrained to IDs already provided in the signal object.
    """

    def __init__(
        self, *, agent_id: str, model: str, api_key: str, base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 15.0, ttl_seconds: int = 300,
    ):
        self.agent_id = str(agent_id).strip()
        self.model = str(model).strip()
        self.api_key = str(api_key)
        self.base_url = str(base_url).rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.ttl_seconds = int(ttl_seconds)
        parsed = urlparse(self.base_url)
        if not self.agent_id or not self.model or not self.api_key:
            raise ValueError("agent_id, model and api_key are required")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError("provider endpoint must use HTTPS, except loopback development endpoints")
        if not parsed.netloc:
            raise ValueError("provider base_url must include a hostname")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 120:
            raise ValueError("timeout_seconds must be in (0,120]")
        if self.ttl_seconds < 1 or self.ttl_seconds > 3600:
            raise ValueError("ttl_seconds must be in [1,3600]")

    async def __call__(self, signal: Mapping[str, Any]) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._call_sync, signal)

    def _call_sync(self, signal: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(signal, Mapping):
            raise ValueError("signal input must be an object")
        signal_id = str(signal.get("signal_id", "")).strip()
        if not signal_id:
            raise ValueError("signal_id is required")
        available = signal.get("available_evidence_refs", [])
        if (not isinstance(available, Sequence) or isinstance(available, (str, bytes))
                or not available or any(not str(ref).strip() for ref in available)):
            raise ValueError("available_evidence_refs must contain pre-validated evidence IDs")
        signal_json = json.dumps(signal, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                  allow_nan=False, default=str)
        if len(signal_json.encode("utf-8")) > 200_000:
            raise ValueError("signal payload exceeds 200 KB limit")

        prompt = (
            "You are one skeptical trading-research agent. Assess only the supplied evidence. "
            "Do not invent prices, news, citations or source IDs. Return one JSON object with exactly "
            "these fields: verdict (APPROVE, ABSTAIN, REJECT, VETO), confidence (number 0..1), "
            "evidence_refs (non-empty array chosen only from available_evidence_refs), "
            "abstain_reason (string), rationale (short factual summary), risks (array of short strings). "
            "APPROVE is not a trade authorization. Prefer ABSTAIN if evidence is insufficient. "
            "Do not output a trade command. Input follows:\n" + signal_json
        )
        endpoint = self.base_url if self.base_url.endswith("/chat/completions") else self.base_url + "/chat/completions"
        body = json.dumps({
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "Return a valid JSON object only; never issue or authorize orders."},
                {"role": "user", "content": prompt},
            ],
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json",
                     "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_body = response.read(256_001)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"provider_http_error:{exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("provider_network_or_timeout_error") from None
        if len(response_body) > 256_000:
            raise RuntimeError("provider_response_too_large")
        try:
            envelope = json.loads(response_body.decode("utf-8"))
            content = envelope["choices"][0]["message"]["content"]
            result = json.loads(content) if isinstance(content, str) else content
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
            raise RuntimeError("provider_response_schema_invalid") from None
        if not isinstance(result, Mapping):
            raise RuntimeError("provider_response_not_json_object")
        verdict = str(result.get("verdict", "")).upper()
        confidence = result.get("confidence")
        try:
            numeric_confidence = float(confidence) if not isinstance(confidence, bool) else float("nan")
        except (TypeError, ValueError, OverflowError):
            numeric_confidence = float("nan")
        evidence_refs = result.get("evidence_refs")
        allowed_refs = {str(ref) for ref in available}
        if (verdict not in {"APPROVE", "ABSTAIN", "REJECT", "VETO"}
                or not 0 <= numeric_confidence <= 1
                or not isinstance(evidence_refs, list) or not evidence_refs
                or any(not isinstance(ref, str) or ref not in allowed_refs for ref in evidence_refs)):
            raise RuntimeError("provider_verdict_or_evidence_invalid")
        return {
            "agent_id": self.agent_id,
            "signal_id": signal_id,
            "verdict_id": "v-" + uuid.uuid4().hex,
            "input_hash": canonical_signal_hash(signal),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": self.ttl_seconds,
            "verdict": verdict,
            "confidence": numeric_confidence,
            "evidence_refs": evidence_refs,
            "abstain_reason": str(result.get("abstain_reason", "")).strip(),
            "rationale": str(result.get("rationale", "")).strip()[:2000],
            "risks": [str(item)[:300] for item in result.get("risks", [])[:20]]
                    if isinstance(result.get("risks", []), list) else [],
        }


def load_agent_specs_from_env():
    """Return AgentSpec records from MT5_AGENT_PROVIDERS JSON and named key env vars."""
    from .agent_runtime import AgentSpec

    raw = os.getenv("MT5_AGENT_PROVIDERS", "")
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("MT5_AGENT_PROVIDERS must be a JSON array") from exc
    if not isinstance(entries, list) or len(entries) > 20:
        raise ValueError("MT5_AGENT_PROVIDERS must be an array with at most 20 entries")
    specs = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("each agent provider configuration must be an object")
        if "api_key" in entry or "secret" in entry:
            raise ValueError("provider secrets must be supplied through environment variables, not inline JSON")
        agent_id = str(entry.get("agent_id", "")).strip()
        group = str(entry.get("independence_group", "")).strip()
        model = str(entry.get("model", "")).strip()
        api_key_env = str(entry.get("api_key_env", "")).strip()
        if not api_key_env or not agent_id or not group or not model:
            raise ValueError("agent_id, independence_group, model and api_key_env are required")
        api_key = os.getenv(api_key_env, "")
        if not api_key:
            raise ValueError(f"required API key environment variable is not set: {api_key_env}")
        agent = OpenAICompatibleAgent(
            agent_id=agent_id, model=model, api_key=api_key,
            base_url=str(entry.get("base_url", "https://api.openai.com/v1")),
            timeout_seconds=float(entry.get("timeout_seconds", 15)),
            ttl_seconds=int(entry.get("ttl_seconds", 300)),
        )
        specs.append(AgentSpec(agent_id, group, agent))
    return specs
