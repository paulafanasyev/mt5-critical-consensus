#!/usr/bin/env python3
"""Run read-only MT5 snapshot analysis with explicitly configured provider agents."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone

from critical_consensus.agent_runtime import collect_agent_verdicts
from critical_consensus.market_data import build_market_snapshot
from redis.asyncio import Redis

from critical_consensus.persistence import PostgresRepository
from critical_consensus.providers import canonical_signal_hash, load_agent_specs_from_env
from critical_consensus.redis_streams import publish_event


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
        if not agents:
            result = {"signal_id": signal_id, "status": "GATHER_MORE", "verdicts": [],
                      "agent_errors": [], "blockers": ["AGENT_PROVIDER_REGISTRY_NOT_CONFIGURED"],
                      "trade_allowed": False}
        else:
            result = await collect_agent_verdicts(
                signal, agents, timeout_seconds=args.timeout,
                minimum_agents=args.minimum_agents,
                minimum_independence_groups=args.minimum_independence_groups,
                required_roles=("analyst", "critic", "chief_review", "risk_review"),
            )
    except Exception as exc:
        # Do not print provider endpoint secrets or exception strings.
        result = {"signal_id": signal_id, "status": "GATHER_MORE", "verdicts": [],
                  "agent_errors": [{"code": type(exc).__name__}],
                  "blockers": ["AGENT_CONFIGURATION_OR_COLLECTION_FAILED"],
                  "trade_allowed": False}
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
                      "audit_event_id": event["event_id"], "redis_publication": redis_status},
                     indent=2, ensure_ascii=False))
    if result.get("status") != "READY_FOR_REVIEW":
        return 2
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
