"""Real Redis/PostgreSQL bridge test, enabled in validation-suite.yml."""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from redis.asyncio import Redis

from critical_consensus.persistence import PostgresRepository
from critical_consensus.redis_streams import (
    ensure_consumer_group, process_message_batch, publish_event, read_new_messages,
    reclaim_and_process_batch,
)


pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL") or not os.getenv("TEST_REDIS_URL"),
    reason="TEST_DATABASE_URL and TEST_REDIS_URL must be configured",
)


@pytest.mark.asyncio
async def test_redis_event_is_committed_before_ack_and_replay_is_idempotent():
    repo = PostgresRepository(os.environ["TEST_DATABASE_URL"])
    await asyncio.to_thread(repo.migrate)
    redis = Redis.from_url(os.environ["TEST_REDIS_URL"], decode_responses=True)
    stream = "cc:integration:" + uuid.uuid4().hex
    group = "test-group"
    consumer = "test-consumer"
    event = {
        "event_id": "integration-" + uuid.uuid4().hex,
        "event_type": "READ_ONLY_MARKET_DATA_TEST",
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload": {"symbol": "EURUSD", "trade_allowed": False},
    }
    try:
        await redis.ping()
        first_id = await publish_event(redis, stream, event)
        second_id = await publish_event(redis, stream, event)
        assert first_id == second_id
        await ensure_consumer_group(redis, stream, group, start_id="0-0")
        messages = await read_new_messages(redis, stream, group, consumer, count=5, block_ms=1000)
        assert len(messages) == 1

        async def handler(message):
            await asyncio.to_thread(repo.persist_event_once, message, consumer_name=group)

        stats = await process_message_batch(redis, stream, group, messages, handler)
        assert stats == {"processed": 1, "retried": 0, "dead_lettered": 0, "invalid": 0}
        pending = await redis.xpending(stream, group)
        assert int(pending["pending"]) == 0
        # A duplicate durable write is acknowledged as already processed, not applied twice.
        assert await asyncio.to_thread(repo.persist_event_once, event, consumer_name=group) is False
    finally:
        await redis.delete(stream, f"cc:published:{stream}:{event['event_id']}")
        await redis.aclose()



@pytest.mark.asyncio
async def test_worker_reclaims_abandoned_pending_event_after_consumer_crash():
    repo = PostgresRepository(os.environ["TEST_DATABASE_URL"])
    await asyncio.to_thread(repo.migrate)
    redis = Redis.from_url(os.environ["TEST_REDIS_URL"], decode_responses=True)
    stream = "cc:reclaim-integration:" + uuid.uuid4().hex
    group = "reclaim-group"
    event = {
        "event_id": "reclaim-" + uuid.uuid4().hex,
        "event_type": "READ_ONLY_RECOVERY_TEST",
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload": {"symbol": "EURUSD", "trade_allowed": False},
    }
    try:
        await redis.ping()
        await redis.xadd(stream, {"event": json.dumps(event, sort_keys=True, separators=(",", ":"))})
        await ensure_consumer_group(redis, stream, group, start_id="0-0")
        abandoned = await read_new_messages(redis, stream, group, "crashed-worker", count=5, block_ms=1000)
        assert len(abandoned) == 1
        assert int((await redis.xpending(stream, group))["pending"]) == 1

        async def persist(message):
            await asyncio.to_thread(repo.persist_event_once, message, consumer_name=group)

        next_cursor, stats = await reclaim_and_process_batch(
            redis, stream, group, "recovery-worker", persist,
            start_id="0-0", min_idle_ms=0, count=5,
        )
        assert next_cursor == "0-0"
        assert stats == {"processed": 1, "retried": 0, "dead_lettered": 0, "invalid": 0}
        assert int((await redis.xpending(stream, group))["pending"]) == 0
        assert await asyncio.to_thread(repo.persist_event_once, event, consumer_name=group) is False
    finally:
        await redis.delete(stream, f"cc:attempts:{stream}:{group}:0-0")
        await redis.aclose()
