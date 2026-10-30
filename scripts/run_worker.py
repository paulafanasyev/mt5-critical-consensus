#!/usr/bin/env python3
"""Persist Redis stream events to PostgreSQL before acknowledging them."""
from __future__ import annotations

import asyncio
import json
import os

from redis.asyncio import Redis

from critical_consensus.persistence import PostgresRepository
from critical_consensus.redis_streams import (\n    ensure_consumer_group, process_message_batch, read_new_messages, reclaim_pending_messages,\n)


async def main() -> None:
    redis_url = os.getenv("REDIS_URL", "")
    if not redis_url:
        raise RuntimeError("REDIS_URL is required")
    stream = os.getenv("CC_STREAM", "cc:signals:v1")
    group = os.getenv("CC_CONSUMER_GROUP", "cc-durable-writer-v1")
    consumer = os.getenv("CC_CONSUMER_NAME", "worker-1")
    repo = PostgresRepository()
    redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        await redis.ping()
        await ensure_consumer_group(redis, stream, group, start_id="0-0")
        await asyncio.to_thread(repo.migrate)
        print(json.dumps({"status": "ready", "stream": stream, "group": group, "consumer": consumer}))
        while True:
            messages = await read_new_messages(redis, stream, group, consumer, count=20, block_ms=5000)
            if not messages:
                continue

            async def persist(event):
                # Durable PostgreSQL commit completes before process_message_batch calls XACK.
                await asyncio.to_thread(repo.persist_event_once, event, consumer_name=group)

            stats = await process_message_batch(redis, stream, group, messages, persist, max_attempts=3)
            print(json.dumps({"stream": stream, **stats}, sort_keys=True))
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
