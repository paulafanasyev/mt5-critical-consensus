#!/usr/bin/env python3
"""Run read-only MT5 snapshot analysis with explicitly configured provider agents."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from critical_consensus.agent_runtime import collect_agent_verdicts
from critical_consensus.market_data import build_market_snapshot
from redis.asyncio import Redis

from critical_consensus.persistence import PostgresRepository
from critical_consensus.providers import canonical_signal_hash, load_agent_specs_from_env
from critical_consensus.redis_streams import publish_event
from critical_consensus.review_service import run_peer_review_cycle


ROOT = Path(__file__).resolve().parents[1]


async def run(args) -> int:
    repo = PostgresRepository()
    repo.migrate()
    bars = await asyncio.to_thread(repo.get_recent_market_bars, args.symbol, args.timeframe, args.bars)
    snapshot = build_market_snapshot(bars)
    snapshot_hash = canonical_signal_hash(snapshot)
    signal_id = "sig-" + hashlib.sha256(snapshot_hash.encode("utf-8")).hexdigest()[:24]
    signal = {
        "signal_id": signal_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "market_snapshot": snapshot,
        "available_evidence_refs": [f"market-bars:{snapshot_hash}"],
        "purpose": "read-only research; no order request",
    }
    try:
        agents = load_agent_specs_from_env()
        policy_path = ROOT / "config" / "certainty_thresholds.yaml"
        certainty_policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        mandatory_agent_ids = certainty_policy.get("mandatory_agents", []) if isinstance(certainty_policy, dict) else []
        if not isinstance(mandatory_agent_ids, list) or not mandatory_agent_ids:
            raise ValueError("certainty policy has no mandatory agent registry")
        if not agents:
            result = {"signal_id": signal_id, "status": "GATHER_MORE", "verdicts": [],
                      "agent_errors": [], "blockers": ["AGENT_PROVIDER_REGISTRY_NOT_CONFIGURED"],
                      "trade_allowed": False}
        else:
            result = await collect_agent_verdicts(
                signal, agents, timeout_seconds=args.timeout,
                minimum_agents=max(args.minimum_agents, len(mandatory_agent_ids)),
                minimum_independence_groups=max(args.minimum_independence_groups, len(mandatory_agent_ids)),
                required_roles=("analyst", "critic", "devil_advocate", "risk_review"),
                required_agent_ids=mandatory_agent_ids,
            )
    except Exception as exc:
        # Do not print provider endpoint secrets or exception strings.
        result = {"signal_id": signal_id, "status": "GATHER_MORE", "verdicts": [],
                  "agent_errors": [{"code": type(exc).__name__}],
                  "blockers": ["AGENT_CONFIGURATION_OR_COLLECTION_FAILED"],
                  "trade_allowed": False}

    if result.get("status") == "READY_FOR_REVIEW":
        try:
            reviewer_registry = [
                (spec.agent_id, spec.run.make_peer_reviewer(signal["available_evidence_refs"]))
                for spec in agents if callable(getattr(spec.run, "make_peer_reviewer", None))
            ]
            resolver_spec = next(
                (spec for spec in agents if spec.role == "devil_advocate"
                 and callable(getattr(spec.run, "make_challenge_resolver", None))),
                None,
            )
            if resolver_spec is None:
                raise ValueError("trusted challenge resolver provider is not configured")
            resolver = resolver_spec.run.make_challenge_resolver(signal["available_evidence_refs"])
            review_cycle = await run_peer_review_cycle(
                result.get("verdicts", []), reviewer_registry,
                allowed_evidence_refs=signal["available_evidence_refs"],
                resolver=resolver, min_reviewers_per_verdict=2, max_resolution_rounds=3,
            )
            result["agent_collection"] = dict(result)
            result["peer_review_cycle"] = review_cycle
            result["peer_reviews"] = review_cycle.get("peer_reviews", [])
            result["challenge_records"] = review_cycle.get("challenge_records", [])
            result["open_challenges"] = review_cycle.get("open_challenges", [])
            result["challenge_resolution_status"] = review_cycle.get("challenge_resolution_status", "MISSING")
            result["status"] = review_cycle.get("status", "GATHER_MORE")
            result["blockers"] = sorted(set(result.get("blockers", []) + review_cycle.get("blockers", [])))
            result["trade_allowed"] = False
        except Exception as exc:
            result["status"] = "GATHER_MORE"
            result["blockers"] = sorted(set(result.get("blockers", []) + [
                "TRUSTED_PEER_REVIEW_OR_CHALLENGE_RESOLUTION_FAILED:" + type(exc).__name__
            ]))
            result["trade_allowed"] = False

    event = {
        "event_id": "agent-collection-" + uuid.uuid4().hex,
        "event_type": "AGENT_COLLECTION_RESULT",
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload": {"signal_id": signal_id, "snapshot_hash": snapshot_hash,
                    "snapshot": snapshot, "collection_result": result},
    }
    # Persist a local durable trace first; then publish the same idempotent event for the Redis worker.
    await asyncio.to_thread(repo.persist_event_once, event, consumer_name="read-only-agent-analysis")
    redis_status = {"status": "NOT_CONFIGURED"}
    redis_url = os.getenv("REDIS_URL", "")
    if redis_url:
        redis = Redis.from_url(redis_url, decode_responses=True)
        try:
            await redis.ping()
            stream = os.getenv("CC_STREAM", "cc:signals:v1")
            stream_id = await publish_event(redis, stream, event)
            redis_status = {"status": "PUBLISHED", "stream": stream, "stream_id": stream_id}
        except Exception as exc:
            # Report only the exception class, never a URL that could contain credentials.
            redis_status = {"status": "FAILED", "error_code": type(exc).__name__}
        finally:
            await redis.aclose()
    print(json.dumps({"market_snapshot": snapshot, "collection": result,
                      "pipeline_stage": "AGENT_COLLECTION_AND_PEER_REVIEW",
                      "deterministic_Chief_Gate_invoked": False,
                      "human_authorization_processed": False,
                      "audit_event_id": event["event_id"], "redis_publication": redis_status},
                     indent=2, ensure_ascii=False))
    if result.get("status") != "READY_FOR_CHIEF":
        return 2
    # READY_FOR_CHIEF means only that the evidence-backed peer-review/challenge ledger is complete.
    # Certainty Gate, deterministic Chief reasoning, Permission Gate and user authorization are separate.
    return 0 if redis_status.get("status") == "PUBLISHED" else 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True, choices=["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"])
    parser.add_argument("--bars", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--minimum-agents", type=int, default=4)
    parser.add_argument("--minimum-independence-groups", type=int, default=3)
    args = parser.parse_args()
    if args.bars < 21 or args.bars > 5000:
        parser.error("--bars must be between 21 and 5000")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
