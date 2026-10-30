"""Redis Streams helpers with idempotent publishing and at-least-once consumers.

Handlers must still be idempotent at the durable sink (for example, a PostgreSQL
inbox table keyed by event_id). Redis transport alone cannot provide exactly-once
side effects across a process crash.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Sequence

STREAM_SIGNALS = "cc:signals:v1"
STREAM_REVIEWS = "cc:reviews:v1"
STREAM_CHALLENGES = "cc:challenges:v1"
STREAM_COMMANDS = "cc:commands:v1"
STREAM_DLQ = "cc:dead-letter:v1"

_REQUIRED_EVENT_FIELDS = {"event_id", "event_type", "schema_version", "created_at", "payload"}

_PUBLISH_LUA = """
-- CC_PUBLISH_EVENT_V2: bind a stable event ID to one canonical payload hash.
local existing = redis.call('GET', KEYS[1])
if existing then
  local existing_id, existing_hash = string.match(existing, '^([^|]+)|([0-9a-f]+)$')
  if not existing_id or string.len(existing_hash) ~= 64 then
    return redis.error_reply('EVENT_ID_DEDUPE_RECORD_LEGACY_OR_CORRUPT')
  end
  if existing_hash ~= ARGV[3] then
    return redis.error_reply('EVENT_ID_PAYLOAD_CONFLICT')
  end
  return existing_id
end
local stream_id = redis.call('XADD', KEYS[2], '*', 'event', ARGV[1])
redis.call('SET', KEYS[1], stream_id .. '|' .. ARGV[3], 'EX', ARGV[2])
return stream_id
"""

_DEAD_LETTER_LUA = """
-- CC_DEAD_LETTER_V1: write once and ACK in one atomic Redis script.
local existing = redis.call('GET', KEYS[1])
if existing then
  redis.call('XACK', KEYS[2], ARGV[1], ARGV[2])
  redis.call('DEL', KEYS[3])
  return existing
end
local dlq_id = redis.call('XADD', KEYS[4], '*', 'dead_letter', ARGV[3])
redis.call('SET', KEYS[1], dlq_id, 'EX', ARGV[4])
redis.call('XACK', KEYS[2], ARGV[1], ARGV[2])
redis.call('DEL', KEYS[3])
return dlq_id
"""

_ATTEMPT_LUA = """
-- CC_INCREMENT_ATTEMPT_V1: increment and refresh the expiry atomically.
local attempt = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[1])
return attempt
"""


def _decode(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    """Reject values that JSON would coerce ambiguously or cannot represent safely."""
    if depth > 32:
        raise ValueError("JSON nesting exceeds 32 levels")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    raise ValueError("value is not a JSON-native type")


def _validate_event(event: Mapping[str, Any]) -> None:
    if not isinstance(event, Mapping):
        raise ValueError("event must be an object")
    missing = _REQUIRED_EVENT_FIELDS - set(event)
    if missing:
        raise ValueError("event missing fields: " + ",".join(sorted(missing)))
    event_id = event.get("event_id")
    event_type = event.get("event_type")
    if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 256:
        raise ValueError("event_id must be a non-empty string of at most 256 characters")
    if not isinstance(event_type, str) or not event_type.strip() or len(event_type) > 128:
        raise ValueError("event_type must be a non-empty string of at most 128 characters")
    if (isinstance(event.get("schema_version"), bool)
            or not isinstance(event.get("schema_version"), int)
            or event["schema_version"] < 1):
        raise ValueError("schema_version must be a positive integer")
    if not isinstance(event.get("payload"), Mapping):
        raise ValueError("payload must be an object")
    created_at = event.get("created_at")
    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError("created_at must be an ISO-8601 string")
    try:
        stamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("created_at must be ISO-8601") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("created_at must include a timezone")
    try:
        _validate_json_value(event)
        canonical = json.dumps(
            dict(event), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError(
            "event must contain only finite JSON values with string keys and depth <= 32"
        ) from exc
    if len(canonical.encode("utf-8")) > 256_000:
        raise ValueError("event payload exceeds 256 KB limit")


async def publish_event(redis_client, stream: str, event: Mapping[str, Any], *, dedupe_ttl_seconds: int = 2_592_000) -> str:
    """Publish once per event ID; reject reuse of that ID for different canonical content."""
    _validate_event(event)
    if not isinstance(stream, str) or not stream.startswith("cc:") or len(stream) > 256:
        raise ValueError("stream must be a non-empty cc: stream name of at most 256 characters")
    if (isinstance(dedupe_ttl_seconds, bool) or not isinstance(dedupe_ttl_seconds, int)
            or dedupe_ttl_seconds < 1):
        raise ValueError("dedupe_ttl_seconds must be a positive integer")
    serialized = json.dumps(
        dict(event), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )
    payload_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    dedupe_key = f"cc:published:{stream}:{event['event_id']}"
    result = await redis_client.eval(
        _PUBLISH_LUA, 2, dedupe_key, stream, serialized, dedupe_ttl_seconds, payload_hash,
    )
    return str(_decode(result))


async def ensure_consumer_group(redis_client, stream: str, group: str, *, start_id: str = "0-0") -> None:
    """Create a stream group once; only suppress Redis' already-exists response."""
    try:
        await redis_client.xgroup_create(stream, group, id=start_id, mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc).upper():
            raise


def _message_event(fields: Mapping[Any, Any]) -> tuple[dict[str, Any] | None, str | None, str]:
    normalized = {_decode(k): _decode(v) for k, v in fields.items()}
    raw = normalized.get("event")
    if not isinstance(raw, str):
        return None, "EVENT_FIELD_MISSING", ""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None, "INVALID_EVENT_JSON", raw
    if not isinstance(parsed, dict):
        return None, "EVENT_JSON_NOT_OBJECT", raw
    try:
        _validate_event(parsed)
    except ValueError as exc:
        # Return a stable reason code rather than logging untrusted input or exception data.
        reason = str(exc).split(":", 1)[0].upper().replace(" ", "_")
        return None, f"SCHEMA_VALIDATION_FAILED:{reason}", raw
    return parsed, None, raw


async def _dead_letter(redis_client, *, stream: str, group: str, message_id: str,
                        event: Mapping[str, Any] | None, raw: str, error_code: str,
                        attempts: int) -> None:
    """Idempotently archive a poison event and ACK it in one atomic Redis operation."""
    raw_hash = "sha256:" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
    dlq_record = {
        "source_stream": stream,
        "source_group": group,
        "source_message_id": message_id,
        "event_id": event.get("event_id", "") if event else "",
        "event_type": event.get("event_type", "") if event else "",
        "event": dict(event) if event else None,
        "raw_hash": raw_hash,
        "error_code": error_code,
        "attempts": attempts,
    }
    serialized = json.dumps(
        dlq_record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )
    dedupe_key = f"cc:dead-lettered:{stream}:{group}:{message_id}"
    attempt_key = f"cc:attempts:{stream}:{group}:{message_id}"
    await redis_client.eval(
        _DEAD_LETTER_LUA, 4, dedupe_key, stream, attempt_key, STREAM_DLQ,
        group, message_id, serialized, 2_592_000,
    )


async def process_message_batch(
    redis_client,
    stream: str,
    group: str,
    messages: Sequence[tuple[Any, Mapping[Any, Any]]],
    handler: Callable[[Mapping[str, Any]], Awaitable[None]],
    *,
    max_attempts: int = 3,
) -> dict[str, int]:
    """Process XREADGROUP/XAUTOCLAIM messages; unacked failures remain pending for retry."""
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    stats = {"processed": 0, "retried": 0, "dead_lettered": 0, "invalid": 0}
    for raw_message_id, fields in messages:
        message_id = str(_decode(raw_message_id))
        event, parse_error, raw = _message_event(fields)
        attempt_key = f"cc:attempts:{stream}:{group}:{message_id}"
        attempt = int(await redis_client.eval(
            _ATTEMPT_LUA, 1, attempt_key, 7 * 24 * 60 * 60,
        ))
        if parse_error:
            await _dead_letter(redis_client, stream=stream, group=group, message_id=message_id,
                               event=None, raw=raw, error_code=parse_error, attempts=attempt)
            stats["dead_lettered"] += 1
            stats["invalid"] += 1
            continue
        try:
            await handler(event)
        except Exception as exc:
            # Persist a coarse code only. Raw exception strings can contain secrets or personal data.
            error_code = f"HANDLER_FAILED:{type(exc).__name__}"
            if attempt >= max_attempts:
                await _dead_letter(redis_client, stream=stream, group=group, message_id=message_id,
                                   event=event, raw=raw, error_code=error_code, attempts=attempt)
                stats["dead_lettered"] += 1
            else:
                stats["retried"] += 1
            continue
        await redis_client.xack(stream, group, message_id)
        await redis_client.delete(attempt_key)
        stats["processed"] += 1
    return stats


async def reclaim_and_process_batch(
    redis_client,
    stream: str,
    group: str,
    consumer: str,
    handler: Callable[[Mapping[str, Any]], Awaitable[None]],
    *,
    start_id: str = "0-0",
    min_idle_ms: int = 30_000,
    count: int = 10,
    max_attempts: int = 3,
) -> tuple[str, dict[str, int]]:
    """Reclaim abandoned pending entries, persist them, then ACK through the common batch path."""
    next_id, messages = await reclaim_pending_messages(
        redis_client, stream, group, consumer,
        min_idle_ms=min_idle_ms, start_id=start_id, count=count,
    )
    if not messages:
        return next_id, {"processed": 0, "retried": 0, "dead_lettered": 0, "invalid": 0}
    stats = await process_message_batch(
        redis_client, stream, group, messages, handler, max_attempts=max_attempts,
    )
    return next_id, stats


async def read_new_messages(redis_client, stream: str, group: str, consumer: str, *,
                            count: int = 10, block_ms: int = 1000) -> list[tuple[str, Mapping[Any, Any]]]:
    """Read new events from a consumer group. Create the group first with ensure_consumer_group."""
    if count < 1 or block_ms < 0:
        raise ValueError("count must be positive and block_ms non-negative")
    records = await redis_client.xreadgroup(group, consumer, {stream: ">"}, count=count, block=block_ms)
    output: list[tuple[str, Mapping[Any, Any]]] = []
    for _stream_name, messages in records or []:
        output.extend((str(_decode(message_id)), fields) for message_id, fields in messages)
    return output


async def reclaim_pending_messages(redis_client, stream: str, group: str, consumer: str, *,
                                   min_idle_ms: int = 30_000, start_id: str = "0-0",
                                   count: int = 10) -> tuple[str, list[tuple[str, Mapping[Any, Any]]]]:
    """Claim idle pending entries after a worker crash, so bounded retries can continue."""
    if min_idle_ms < 0 or count < 1:
        raise ValueError("min_idle_ms must be non-negative and count positive")
    result = await redis_client.xautoclaim(stream, group, consumer, min_idle_ms, start_id, count=count)
    if not isinstance(result, (tuple, list)) or len(result) < 2:
        raise RuntimeError("unexpected XAUTOCLAIM response")
    next_id = str(_decode(result[0]))
    messages = [(str(_decode(message_id)), fields) for message_id, fields in result[1]]
    return next_id, messages
