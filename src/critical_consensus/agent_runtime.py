"""Timeout-bounded agent collection with explicit provenance limitations.

This module coordinates registered provider callables. It does not instantiate an LLM
or prove that two providers are statistically independent; independence groups are
declared metadata and must be audited separately.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence

AgentCallable = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


class AgentSpec:
    ALLOWED_ROLES = {"analyst", "critic", "chief_review", "risk_review", "devil_advocate"}

    def __init__(
        self, agent_id: str, independence_group: str, run: AgentCallable, *,
        role: str = "analyst", provider_identity: str | None = None, provider_origin: str | None = None,
    ):
        self.agent_id = str(agent_id).strip()
        self.independence_group = str(independence_group).strip()
        self.role = str(role).strip().lower()
        self.run = run
        identity = provider_identity or getattr(run, "provider_identity", "")
        origin = provider_origin or getattr(run, "provider_origin", "")
        self.provider_identity = str(identity).strip().lower()
        self.provider_origin = str(origin).strip().lower().rstrip("/")
        if not self.agent_id or not self.independence_group or not callable(run):
            raise ValueError("agent_id, independence_group and run callable are required")
        if self.role not in self.ALLOWED_ROLES:
            raise ValueError("unsupported agent role")


def _valid_stamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _validate_result(
    result: Mapping[str, Any], spec: AgentSpec, signal_id: str, expected_input_hash: str,
    *, now: datetime, max_future_skew_seconds: int = 30,
) -> list[str]:
    blockers: list[str] = []
    if str(result.get("agent_id", "")).strip() != spec.agent_id:
        blockers.append("AGENT_ID_MISMATCH")
    if str(result.get("signal_id", "")).strip() != signal_id:
        blockers.append("SIGNAL_ID_MISMATCH")
    if not str(result.get("verdict_id", "")).strip():
        blockers.append("VERDICT_ID_MISSING")
    supplied_hash = str(result.get("input_hash", ""))
    if re.fullmatch(r"sha256:[0-9a-f]{64}", supplied_hash) is None:
        blockers.append("INPUT_HASH_INVALID")
    elif supplied_hash != expected_input_hash:
        blockers.append("INPUT_HASH_DOES_NOT_MATCH_COLLECTED_SIGNAL")
    confidence = result.get("confidence")
    if isinstance(confidence, bool):
        confidence = None
    try:
        confidence = float(confidence)
    except (TypeError, ValueError, OverflowError):
        confidence = None
    if confidence is None or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        blockers.append("CONFIDENCE_INVALID")
    if str(result.get("verdict", "")).upper() not in {"APPROVE", "ABSTAIN", "REJECT", "VETO", "HOLD"}:
        blockers.append("VERDICT_INVALID")
    created = _valid_stamp(result.get("created_at"))
    if created is None:
        blockers.append("CREATED_AT_INVALID")
    else:
        age = (now - created).total_seconds()
        if age < -max_future_skew_seconds:
            blockers.append("CREATED_AT_IN_FUTURE")
    ttl = result.get("ttl_seconds")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
        blockers.append("TTL_INVALID")
    elif created is not None and (now - created).total_seconds() > ttl:
        blockers.append("VERDICT_EXPIRED")
    refs = result.get("evidence_refs")
    if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)) or not refs or any(not str(x).strip() for x in refs):
        blockers.append("EVIDENCE_REFERENCES_MISSING")
    return blockers


async def collect_agent_verdicts(
    signal: Mapping[str, Any], agents: Sequence[AgentSpec], *,
    timeout_seconds: float = 15.0, minimum_agents: int = 3, minimum_independence_groups: int = 3,
    required_roles: Sequence[str] = (), now: datetime | None = None,
) -> dict[str, Any]:
    """Collect verdicts concurrently and fail closed on timeouts, malformed output or insufficient coverage."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        return {"status": "GATHER_MORE", "verdicts": [], "blockers": ["INVALID_CURRENT_TIME"], "trade_allowed": False}
    current = current.astimezone(timezone.utc)
    signal_id = str(signal.get("signal_id", "")).strip() if isinstance(signal, Mapping) else ""
    if not signal_id:
        return {"status": "GATHER_MORE", "verdicts": [], "blockers": ["SIGNAL_ID_MISSING"], "trade_allowed": False}
    try:
        canonical_signal = json.dumps(signal, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False, allow_nan=False, default=str)
        expected_input_hash = "sha256:" + hashlib.sha256(canonical_signal.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, OverflowError):
        return {"status": "GATHER_MORE", "verdicts": [], "blockers": ["SIGNAL_INPUT_NOT_CANONICALLY_HASHABLE"],
                "trade_allowed": False}
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or minimum_agents < 1 or minimum_independence_groups < 1:
        return {"status": "GATHER_MORE", "verdicts": [], "blockers": ["INVALID_COLLECTION_POLICY"], "trade_allowed": False}
    if not isinstance(agents, Sequence) or not agents or any(not isinstance(agent, AgentSpec) for agent in agents):
        return {"status": "GATHER_MORE", "verdicts": [], "blockers": ["AGENT_REGISTRY_INVALID"], "trade_allowed": False}
    if (not isinstance(required_roles, Sequence) or isinstance(required_roles, (str, bytes))
            or any(str(role).strip().lower() not in AgentSpec.ALLOWED_ROLES for role in required_roles)):
        return {"status": "GATHER_MORE", "verdicts": [], "blockers": ["REQUIRED_AGENT_ROLES_POLICY_INVALID"], "trade_allowed": False}
    required_role_set = {str(role).strip().lower() for role in required_roles}
    ids = [agent.agent_id for agent in agents]
    if len(set(ids)) != len(ids):
        return {"status": "GATHER_MORE", "verdicts": [], "blockers": ["DUPLICATE_AGENT_ID"], "trade_allowed": False}

    if minimum_independence_groups > 1:
        missing_identity = [agent.agent_id for agent in agents if not agent.provider_identity]
        missing_origin = [agent.agent_id for agent in agents if not agent.provider_origin]
        if missing_identity or missing_origin:
            blockers = []
            if missing_identity:
                blockers.append("PROVIDER_MODEL_IDENTITY_MISSING")
            if missing_origin:
                blockers.append("PROVIDER_ORIGIN_MISSING")
            return {
                "signal_id": signal_id, "status": "GATHER_MORE", "verdicts": [],
                "agent_errors": [{"agent_id": agent_id, "code": "PROVIDER_METADATA_MISSING"}
                                 for agent_id in sorted(set(missing_identity + missing_origin))],
                "valid_agent_count": 0, "declared_independence_group_count": 0,
                "provider_origin_count": len({agent.provider_origin for agent in agents if agent.provider_origin}),
                "provider_identity_count": len({agent.provider_identity for agent in agents if agent.provider_identity}),
                "independence_evidence": "CONFIGURATION_CHECKED_STATISTICAL_INDEPENDENCE_NOT_VERIFIED",
                "blockers": blockers, "trade_allowed": False,
            }
        origins = [agent.provider_origin for agent in agents]
        identities = [agent.provider_identity for agent in agents]
        duplicate_origins = sorted({origin for origin in origins if origins.count(origin) > 1})
        duplicate_identities = sorted({identity for identity in identities if identities.count(identity) > 1})
        if duplicate_origins or duplicate_identities:
            blockers = []
            if duplicate_origins:
                blockers.append("PROVIDER_ORIGIN_REUSED")
            if duplicate_identities:
                blockers.append("PROVIDER_MODEL_IDENTITY_REUSED")
            return {
                "signal_id": signal_id, "status": "GATHER_MORE", "verdicts": [],
                "agent_errors": [], "valid_agent_count": 0,
                "declared_independence_group_count": len({agent.independence_group for agent in agents}),
                "provider_origin_count": len(set(origins)), "provider_identity_count": len(set(identities)),
                "duplicate_provider_origins": duplicate_origins,
                "duplicate_provider_identities": duplicate_identities,
                "independence_evidence": "CONFIGURATION_CHECKED_STATISTICAL_INDEPENDENCE_NOT_VERIFIED",
                "blockers": blockers, "trade_allowed": False,
            }

    async def one(spec: AgentSpec) -> tuple[AgentSpec, Mapping[str, Any] | None, str | None]:
        try:
            result = await asyncio.wait_for(spec.run(signal), timeout=timeout_seconds)
            if not isinstance(result, Mapping):
                return spec, None, "AGENT_RESULT_NOT_OBJECT"
            reasons = _validate_result(result, spec, signal_id, expected_input_hash, now=current)
            if reasons:
                return spec, None, ",".join(sorted(set(reasons)))
            return spec, dict(result), None
        except asyncio.TimeoutError:
            return spec, None, "AGENT_TIMEOUT"
        except Exception as exc:
            # Do not expose provider exception text; it may contain prompts, tokens or user data.
            return spec, None, f"AGENT_PROVIDER_ERROR:{type(exc).__name__}"

    results = await asyncio.gather(*(one(agent) for agent in agents))
    verdicts: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for spec, result, error in results:
        if result is not None:
            result["declared_independence_group"] = spec.independence_group
            result["provider_origin"] = spec.provider_origin
            result["provider_identity"] = spec.provider_identity
            result["role"] = spec.role
            result["confidence_kind"] = "UNCALIBRATED_SUBJECTIVE"
            verdicts.append(result)
        else:
            errors.append({"agent_id": spec.agent_id, "code": error or "AGENT_RESULT_INVALID"})
    groups = {str(v["declared_independence_group"]) for v in verdicts}
    blockers: list[str] = []
    if len(verdicts) < minimum_agents:
        blockers.append("INSUFFICIENT_VALID_AGENT_VERDICTS")
    if len(groups) < minimum_independence_groups:
        blockers.append("INSUFFICIENT_DECLARED_INDEPENDENCE_GROUPS")
    if errors:
        blockers.append("ONE_OR_MORE_AGENTS_FAILED")
    present_roles = {str(v.get("role", "analyst")) for v in verdicts}
    missing_roles = sorted(required_role_set - present_roles)
    if missing_roles:
        blockers.append("REQUIRED_AGENT_ROLES_MISSING:" + ",".join(missing_roles))
    return {
        "signal_id": signal_id,
        "status": "READY_FOR_REVIEW" if not blockers else "GATHER_MORE",
        "verdicts": verdicts, "agent_errors": errors, "valid_agent_count": len(verdicts),
        "declared_independence_group_count": len(groups),
        "agent_roles": {role: sum(1 for verdict in verdicts if verdict.get("role") == role)
                        for role in sorted(AgentSpec.ALLOWED_ROLES)},
        "required_roles": sorted(required_role_set),
        "missing_roles": missing_roles,
        "provider_origin_count": len({agent.provider_origin for agent in agents if agent.provider_origin}),
        "provider_identity_count": len({agent.provider_identity for agent in agents if agent.provider_identity}),
        "independence_evidence": "CONFIGURATION_CHECKED_STATISTICAL_INDEPENDENCE_NOT_VERIFIED",
        "blockers": blockers, "trade_allowed": False,
    }
