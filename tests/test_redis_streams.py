import json

import pytest

from critical_consensus.redis_streams import (
    STREAM_DLQ,
    ensure_consumer_group,
    process_message_batch,
    publish_event,
)


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.streams = {}
        self.acks = []
        self.groups = set()
        self.counter = 0

    async def eval(self, script, numkeys, *parts):
        if "CC_PUBLISH_EVENT_V2" in script:
            dedupe_key, stream, serialized, ttl, payload_hash = parts
            existing = self.values.get(dedupe_key)
            if existing:
                try:
                    existing_id, existing_hash = existing.split("|", 1)
                except ValueError:
                    raise RuntimeError("EVENT_ID_DEDUPE_RECORD_LEGACY_OR_CORRUPT")
                if len(existing_hash) != 64:
                    raise RuntimeError("EVENT_ID_DEDUPE_RECORD_LEGACY_OR_CORRUPT")
                if existing_hash != payload_hash:
                    raise RuntimeError("EVENT_ID_PAYLOAD_CONFLICT")
                return existing_id
            self.counter += 1
            stream_id = f"{self.counter}-0"
            self.streams.setdefault(stream, []).append((stream_id, {"event": serialized}))
            self.values[dedupe_key] = f"{stream_id}|{payload_hash}"
            return stream_id
        if "CC_DEAD_LETTER_V1" in script:
            dedupe_key, source_stream, attempt_key, dlq_stream, group, message_id, serialized, ttl = parts
            existing = self.values.get(dedupe_key)
            if existing:
                await self.xack(source_stream, group, message_id)
                await self.delete(attempt_key)
                return existing
            dlq_id = await self.xadd(dlq_stream, {"dead_letter": serialized})
            self.values[dedupe_key] = dlq_id
            await self.xack(source_stream, group, message_id)
            await self.delete(attempt_key)
            return dlq_id
        raise AssertionError("unexpected Redis Lua script")

    async def xgroup_create(self, stream, group, id="0-0", mkstream=True):
        if (stream, group) in self.groups:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")
        self.groups.add((stream, group))

    async def xadd(self, stream, fields):
        self.counter += 1
        self.streams.setdefault(stream, []).append((f"{self.counter}-0", fields))
        return f"{self.counter}-0"

    async def xack(self, stream, group, message_id):
        self.acks.append((stream, group, message_id))
        return 1

    async def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    async def expire(self, key, seconds):
        return True

    async def delete(self, key):
        self.values.pop(key, None)
        return 1


def event(event_id="e-1"):
    return {"event_id": event_id, "event_type": "SIGNAL", "schema_version": 1,
            "created_at": "2026-10-29T12:00:00+00:00", "payload": {"signal_id": "sig-1"}}


@pytest.mark.asyncio
async def test_publish_event_is_idempotent_for_event_id():
    redis = FakeRedis()
    first = await publish_event(redis, "cc:signals:v1", event())
    second = await publish_event(redis, "cc:signals:v1", event())
    assert first == second
    assert len(redis.streams["cc:signals:v1"]) == 1


@pytest.mark.asyncio
async def test_publish_event_rejects_timezone_naive_timestamp():
    bad = event()
    bad["created_at"] = "2026-10-29T12:00:00"
    with pytest.raises(ValueError, match="timezone"):
        await publish_event(FakeRedis(), "cc:signals:v1", bad)


@pytest.mark.asyncio
async def test_consumer_group_creation_ignores_only_busygroup():
    redis = FakeRedis()
    await ensure_consumer_group(redis, "cc:signals:v1", "workers")
    await ensure_consumer_group(redis, "cc:signals:v1", "workers")
    assert ("cc:signals:v1", "workers") in redis.groups


@pytest.mark.asyncio
async def test_failed_handler_goes_to_dlq_after_bounded_attempts():
    redis = FakeRedis()
    message = ("11-0", {"event": json.dumps(event())})

    async def broken_handler(_event):
        raise RuntimeError("secret text must not be logged")

    for attempt in (1, 2):
        stats = await process_message_batch(redis, "cc:signals:v1", "workers", [message], broken_handler, max_attempts=3)
        assert stats["retried"] == 1
        assert not stats["dead_lettered"]
    stats = await process_message_batch(redis, "cc:signals:v1", "workers", [message], broken_handler, max_attempts=3)
    assert stats["dead_lettered"] == 1
    assert redis.acks == [("cc:signals:v1", "workers", "11-0")]
    dlq_json = redis.streams[STREAM_DLQ][0][1]["dead_letter"]
    assert "RuntimeError" in json.loads(dlq_json)["error_code"]
    assert "secret text" not in dlq_json


@pytest.mark.asyncio
async def test_invalid_json_is_dead_lettered_and_acked():
    redis = FakeRedis()
    stats = await process_message_batch(redis, "cc:signals:v1", "workers", [("12-0", {"event": "not-json"})],
                                        lambda _event: None)
    assert stats["invalid"] == 1
    assert stats["dead_lettered"] == 1
    assert redis.acks == [("cc:signals:v1", "workers", "12-0")]


@pytest.mark.asyncio
async def test_publish_event_rejects_reused_id_with_different_payload():
    redis = FakeRedis()
    await publish_event(redis, "cc:signals:v1", event())
    changed = event()
    changed["payload"] = {"signal_id": "different-signal"}
    with pytest.raises(RuntimeError, match="EVENT_ID_PAYLOAD_CONFLICT"):
        await publish_event(redis, "cc:signals:v1", changed)
    assert len(redis.streams["cc:signals:v1"]) == 1


@pytest.mark.asyncio
async def test_publish_event_rejects_non_finite_json_values():
    bad = event()
    bad["payload"] = {"confidence": float("nan")}
    with pytest.raises(ValueError, match="finite JSON"):
        await publish_event(FakeRedis(), "cc:signals:v1", bad)


@pytest.mark.asyncio
async def test_publish_event_rejects_non_string_event_id():
    bad = event()
    bad["event_id"] = 17
    with pytest.raises(ValueError, match="event_id"):
        await publish_event(FakeRedis(), "cc:signals:v1", bad)


@pytest.mark.asyncio
async def test_dead_letter_is_idempotent_for_same_source_message():
    redis = FakeRedis()
    message = ("22-0", {"event": json.dumps(event())})

    async def broken_handler(_event):
        raise RuntimeError("boom")

    for _ in range(3):
        stats = await process_message_batch(
            redis, "cc:signals:v1", "workers", [message], broken_handler, max_attempts=1,
        )
        assert stats["dead_lettered"] == 1
    dead_letters = redis.streams[STREAM_DLQ]
    assert len(dead_letters) == 1
    assert redis.acks.count(("cc:signals:v1", "workers", "22-0")) == 2
