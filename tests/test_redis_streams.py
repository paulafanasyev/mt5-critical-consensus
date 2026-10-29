import json

import pytest

from critical_consensus.redis_streams import (
    STREAM_DLQ,
    ensure_consumer_group,
    process_message_batch,
    read_new_messages,
    reclaim_pending_messages,
    publish_event,
)


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.streams = {}
        self.acks = []
        self.groups = set()
        self.counter = 0

    async def eval(self, script, numkeys, dedupe_key, stream, serialized, ttl):
        if dedupe_key in self.values:
            stream_id, old_serialized = self.values[dedupe_key]
            if old_serialized != serialized:
                raise RuntimeError("EVENT_ID_COLLISION: event_id reused with different content")
            return stream_id
        self.counter += 1
        stream_id = f"{self.counter}-0"
        self.streams.setdefault(stream, []).append((stream_id, {"event": serialized}))
        self.values[dedupe_key] = (stream_id, serialized)
        return stream_id

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
async def test_publish_event_rejects_reused_event_id_with_different_content():
    redis = FakeRedis()
    first_event = event("same-id")
    await publish_event(redis, "cc:signals:v1", first_event)
    altered = event("same-id")
    altered["payload"] = {"signal_id": "sig-2"}
    with pytest.raises(RuntimeError, match="EVENT_ID_COLLISION"):
        await publish_event(redis, "cc:signals:v1", altered)
    assert len(redis.streams["cc:signals:v1"]) == 1


@pytest.mark.parametrize("field,value", [
    ("event_id", 123),
    ("event_id", {"id": "e-1"}),
    ("event_type", ["SIGNAL"]),
    ("created_at", 1793280000),
    ("created_at", "2026-10-29 12:00:00+00:00"),
    ("created_at", "2026-10-29T12:00:00"),
])
@pytest.mark.asyncio
async def test_publish_event_rejects_coerced_or_non_rfc3339_fields(field, value):
    malformed = event()
    malformed[field] = value
    with pytest.raises(ValueError):
        await publish_event(FakeRedis(), "cc:signals:v1", malformed)


@pytest.mark.asyncio
async def test_publish_event_rejects_noninteger_or_oversized_dedupe_ttl():
    for ttl in (True, 1.5, "3600", 0, 604801):
        with pytest.raises(ValueError, match="dedupe_ttl_seconds"):
            await publish_event(FakeRedis(), "cc:signals:v1", event(), dedupe_ttl_seconds=ttl)


@pytest.mark.parametrize("bad_payload", [
    {"value": float("nan")},
    {"value": float("inf")},
    {"value": object()},
])
@pytest.mark.asyncio
async def test_publish_event_rejects_non_json_payload_values(bad_payload):
    malformed = event()
    malformed["payload"] = bad_payload
    with pytest.raises(ValueError, match="strict JSON"):
        await publish_event(FakeRedis(), "cc:signals:v1", malformed)


@pytest.mark.asyncio
async def test_publish_event_rejects_oversized_envelope():
    malformed = event()
    malformed["payload"] = {"blob": "x" * 1_100_000}
    with pytest.raises(ValueError, match="1048576-byte limit"):
        await publish_event(FakeRedis(), "cc:signals:v1", malformed)


@pytest.mark.asyncio
async def test_publish_event_accepts_fractional_rfc3339_timestamp():
    valid = event()
    valid["created_at"] = "2026-10-29T12:00:00.123456Z"
    result = await publish_event(FakeRedis(), "cc:signals:v1", valid)
    assert result


@pytest.mark.asyncio
@pytest.mark.parametrize("max_attempts", [True, 1.5, "3", 0, 101])
async def test_process_message_batch_rejects_invalid_retry_bounds(max_attempts):
    with pytest.raises(ValueError, match="max_attempts"):
        await process_message_batch(FakeRedis(), "cc:signals:v1", "workers", [],
                                    lambda _event: None, max_attempts=max_attempts)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [True, 1.5, "10", 0, 1001])
async def test_read_new_messages_rejects_invalid_count_before_redis_call(count):
    with pytest.raises(ValueError, match="count"):
        await read_new_messages(FakeRedis(), "cc:signals:v1", "workers", "consumer-1", count=count)


@pytest.mark.asyncio
@pytest.mark.parametrize("block_ms", [True, 1.5, "100", -1, 60001])
async def test_read_new_messages_rejects_invalid_block_timeout(block_ms):
    with pytest.raises(ValueError, match="block_ms"):
        await read_new_messages(FakeRedis(), "cc:signals:v1", "workers", "consumer-1", block_ms=block_ms)


@pytest.mark.asyncio
@pytest.mark.parametrize("min_idle_ms", [True, 1.5, "30000", -1, 86400001])
async def test_reclaim_pending_messages_rejects_invalid_idle_bound(min_idle_ms):
    with pytest.raises(ValueError, match="min_idle_ms"):
        await reclaim_pending_messages(FakeRedis(), "cc:signals:v1", "workers", "consumer-1",
                                       min_idle_ms=min_idle_ms)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [True, 1.5, "10", 0, 1001])
async def test_reclaim_pending_messages_rejects_invalid_count(count):
    with pytest.raises(ValueError, match="count"):
        await reclaim_pending_messages(FakeRedis(), "cc:signals:v1", "workers", "consumer-1", count=count)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream,group", [
    ("signals:v1", "workers"),
    ("cc:signals:v1", ""),
    ("cc:signals:v1", "bad\ngroup"),
    ("cc:signals:v1", "bad\rgroup"),
    ("cc:signals:v1", "g" * 129),
])
async def test_consumer_group_creation_rejects_invalid_stream_or_group(stream, group):
    with pytest.raises(ValueError):
        await ensure_consumer_group(FakeRedis(), stream, group)


@pytest.mark.asyncio
async def test_process_message_batch_rejects_oversized_batch_and_noncallable_handler():
    with pytest.raises(ValueError, match="at most"):
        await process_message_batch(FakeRedis(), "cc:signals:v1", "workers",
                                    [None] * 1001, lambda _event: None)
    with pytest.raises(ValueError, match="handler"):
        await process_message_batch(FakeRedis(), "cc:signals:v1", "workers",
                                    [], None)


@pytest.mark.asyncio
async def test_consumer_dead_letters_oversized_stream_event_without_calling_handler():
    redis = FakeRedis()
    raw = json.dumps({**event(), "payload": {"blob": "x" * 1_100_000}})
    called = False

    async def handler(_event):
        nonlocal called
        called = True

    stats = await process_message_batch(
        redis, "cc:signals:v1", "workers", [("13-0", {"event": raw})], handler
    )
    assert stats["invalid"] == 1
    assert stats["dead_lettered"] == 1
    assert stats["processed"] == 0
    assert called is False
    assert redis.acks == [("cc:signals:v1", "workers", "13-0")]
    dlq = json.loads(redis.streams[STREAM_DLQ][0][1]["dead_letter"])
    assert dlq["error_code"] == "EVENT_TOO_LARGE"
    assert dlq["raw_hash"].startswith("sha256:")
    assert "x" * 100 not in redis.streams[STREAM_DLQ][0][1]["dead_letter"]


@pytest.mark.parametrize("raw", [
    '{"event_id":"e-1","event_type":"SIGNAL","schema_version":1,'
    '"created_at":"2026-10-29T12:00:00Z","payload":{"value":NaN}}',
    '{"event_id":"e-1","event_id":"e-2","event_type":"SIGNAL","schema_version":1,'
    '"created_at":"2026-10-29T12:00:00Z","payload":{"signal_id":"sig-1"}}',
    '{"event_id":"e-1","event_type":"SIGNAL","schema_version":1,'
    '"created_at":"2026-10-29T12:00:00Z","payload":{"signal_id":"sig-1","signal_id":"sig-2"}}',
])
@pytest.mark.asyncio
async def test_consumer_dead_letters_non_strict_json_before_handler(raw):
    redis = FakeRedis()
    called = False

    async def handler(_event):
        nonlocal called
        called = True

    stats = await process_message_batch(
        redis, "cc:signals:v1", "workers", [("14-0", {"event": raw})], handler
    )
    assert stats["invalid"] == 1
    assert stats["dead_lettered"] == 1
    assert stats["processed"] == 0
    assert called is False
    assert redis.acks == [("cc:signals:v1", "workers", "14-0")]
    dlq = json.loads(redis.streams[STREAM_DLQ][0][1]["dead_letter"])
    assert dlq["error_code"] == "NON_STRICT_EVENT_JSON"
