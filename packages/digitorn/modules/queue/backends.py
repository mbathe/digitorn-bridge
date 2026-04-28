"""Queue backend abstraction - InMemory and Redis Streams implementations."""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .message import QueueMessage

logger = logging.getLogger(__name__)


@dataclass
class QueueStats:
    """Queue statistics."""

    name: str
    depth: int = 0
    dead_letter_count: int = 0
    total_published: int = 0
    total_consumed: int = 0
    consumer_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "depth": self.depth,
            "dead_letter_count": self.dead_letter_count,
            "total_published": self.total_published,
            "total_consumed": self.total_consumed,
            "consumer_count": self.consumer_count,
        }


@runtime_checkable
class QueueBackend(Protocol):
    """Queue backend protocol."""

    async def create_queue(self, name: str, config: dict[str, Any] | None = None) -> None: ...
    async def delete_queue(self, name: str) -> bool: ...
    async def publish(self, name: str, message: QueueMessage) -> str: ...
    async def receive(self, name: str, batch_size: int = 1, timeout: float = 5.0) -> list[QueueMessage]: ...
    async def ack(self, name: str, message_ids: list[str]) -> int: ...
    async def nack(self, name: str, message_ids: list[str], requeue: bool = True) -> int: ...
    async def peek(self, name: str, count: int = 5) -> list[QueueMessage]: ...
    async def stats(self, name: str) -> QueueStats: ...
    async def list_queues(self) -> list[str]: ...
    async def purge(self, name: str) -> int: ...
    async def dead_letter(self, name: str, count: int = 10) -> list[QueueMessage]: ...
    async def close(self) -> None: ...


@dataclass(order=True)
class _PrioritizedMessage:
    """Heap entry for priority queue."""

    priority: int
    timestamp: float
    message: QueueMessage = field(compare=False)


class InMemoryQueueBackend:
    """In-memory queue backend using asyncio queues and heaps.

    Suitable for single-process development and testing. Messages are
    lost on process restart.
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[_PrioritizedMessage]] = {}
        self._dlq: dict[str, list[QueueMessage]] = {}
        self._unacked: dict[str, dict[str, QueueMessage]] = {}  # queue -> {ack_id -> msg}
        self._events: dict[str, asyncio.Event] = {}
        self._stats: dict[str, QueueStats] = {}
        self._configs: dict[str, dict[str, Any]] = {}
        self._delayed: dict[str, list[QueueMessage]] = {}

    async def create_queue(self, name: str, config: dict[str, Any] | None = None) -> None:
        if name not in self._queues:
            self._queues[name] = []
            self._dlq[name] = []
            self._unacked[name] = {}
            self._events[name] = asyncio.Event()
            self._stats[name] = QueueStats(name=name)
            self._configs[name] = config or {}
            self._delayed[name] = []

    async def delete_queue(self, name: str) -> bool:
        if name in self._queues:
            del self._queues[name]
            self._dlq.pop(name, None)
            self._unacked.pop(name, None)
            self._events.pop(name, None)
            self._stats.pop(name, None)
            self._configs.pop(name, None)
            self._delayed.pop(name, None)
            return True
        return False

    async def publish(self, name: str, message: QueueMessage) -> str:
        if name not in self._queues:
            await self.create_queue(name)
        message.queue = name
        if message.is_delayed():
            self._delayed[name].append(message)
        else:
            heapq.heappush(self._queues[name], _PrioritizedMessage(message.priority, message.timestamp, message))
            self._events[name].set()
        self._stats.setdefault(name, QueueStats(name=name)).total_published += 1
        return message.id

    async def receive(self, name: str, batch_size: int = 1, timeout: float = 5.0) -> list[QueueMessage]:
        if name not in self._queues:
            return []
        self._promote_delayed(name)
        messages = []
        deadline = time.monotonic() + timeout
        while len(messages) < batch_size:
            if self._queues[name]:
                entry = heapq.heappop(self._queues[name])
                msg = entry.message
                msg.attempts += 1
                ack_id = str(uuid.uuid4())
                msg.ack_id = ack_id
                self._unacked[name][ack_id] = msg
                messages.append(msg)
                self._stats[name].total_consumed += 1
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._events[name].clear()
                try:
                    await asyncio.wait_for(self._events[name].wait(), timeout=min(remaining, 1.0))
                except asyncio.TimeoutError:
                    if time.monotonic() >= deadline:
                        break
        return messages

    async def ack(self, name: str, message_ids: list[str]) -> int:
        count = 0
        unacked = self._unacked.get(name, {})
        for mid in message_ids:
            if mid in unacked:
                del unacked[mid]
                count += 1
        return count

    async def nack(self, name: str, message_ids: list[str], requeue: bool = True) -> int:
        count = 0
        unacked = self._unacked.get(name, {})
        for mid in message_ids:
            msg = unacked.pop(mid, None)
            if msg is None:
                continue
            count += 1
            if requeue and msg.attempts < msg.max_retries:
                msg.ack_id = None
                heapq.heappush(self._queues[name], _PrioritizedMessage(msg.priority, msg.timestamp, msg))
                self._events[name].set()
            else:
                self._dlq.setdefault(name, []).append(msg)
                self._stats[name].dead_letter_count = len(self._dlq[name])
        return count

    async def peek(self, name: str, count: int = 5) -> list[QueueMessage]:
        if name not in self._queues:
            return []
        self._promote_delayed(name)
        entries = sorted(self._queues[name])[:count]
        return [e.message for e in entries]

    async def stats(self, name: str) -> QueueStats:
        if name not in self._stats:
            return QueueStats(name=name)
        s = self._stats[name]
        s.depth = len(self._queues.get(name, []))
        s.dead_letter_count = len(self._dlq.get(name, []))
        return s

    async def list_queues(self) -> list[str]:
        return sorted(self._queues.keys())

    async def purge(self, name: str) -> int:
        if name not in self._queues:
            return 0
        count = len(self._queues[name])
        self._queues[name] = []
        self._delayed[name] = []
        return count

    async def dead_letter(self, name: str, count: int = 10) -> list[QueueMessage]:
        return self._dlq.get(name, [])[:count]

    async def close(self) -> None:
        self._queues.clear()
        self._dlq.clear()
        self._unacked.clear()

    def _promote_delayed(self, name: str) -> None:
        now = time.time()
        delayed = self._delayed.get(name, [])
        remaining = []
        for msg in delayed:
            if msg.delay_until and msg.delay_until <= now:
                heapq.heappush(self._queues[name], _PrioritizedMessage(msg.priority, msg.timestamp, msg))
                self._events[name].set()
            else:
                remaining.append(msg)
        self._delayed[name] = remaining


class RedisQueueBackend:
    """Redis Streams-backed queue backend for production multi-worker deployments.

    Uses XADD/XREADGROUP/XACK for consumer group semantics with
    at-least-once delivery. Dead-letter via separate streams.
    """

    def __init__(self, app_id: str, url: str = "redis://localhost:6379/0") -> None:
        import redis.asyncio as aioredis

        self._app_id = app_id
        self._redis = aioredis.Redis.from_url(url, decode_responses=True)
        self._prefix = f"q:{app_id}:"
        self._dlq_prefix = f"dlq:{app_id}:"
        self._stats_prefix = f"qs:{app_id}:"
        self._group = f"cg:{app_id}"
        self._consumer = f"worker:{uuid.uuid4().hex[:8]}"
        self._created_groups: set[str] = set()

    def _stream_key(self, name: str) -> str:
        return self._prefix + name

    def _dlq_key(self, name: str) -> str:
        return self._dlq_prefix + name

    async def _ensure_group(self, name: str) -> None:
        if name in self._created_groups:
            return
        try:
            await self._redis.xgroup_create(self._stream_key(name), self._group, id="0", mkstream=True)
        except Exception:
            pass  # group already exists
        self._created_groups.add(name)

    async def create_queue(self, name: str, config: dict[str, Any] | None = None) -> None:
        await self._ensure_group(name)
        if config:
            await self._redis.hset(self._stats_prefix + name, mapping={"config": json.dumps(config)})

    async def delete_queue(self, name: str) -> bool:
        count = await self._redis.delete(self._stream_key(name), self._dlq_key(name), self._stats_prefix + name)
        self._created_groups.discard(name)
        return count > 0

    async def publish(self, name: str, message: QueueMessage) -> str:
        await self._ensure_group(name)
        message.queue = name
        fields = {
            "id": message.id,
            "body": json.dumps(message.body),
            "headers": json.dumps(message.headers),
            "priority": str(message.priority),
            "timestamp": str(message.timestamp),
            "attempts": str(message.attempts),
            "max_retries": str(message.max_retries),
        }
        if message.delay_until:
            fields["delay_until"] = str(message.delay_until)
        await self._redis.xadd(self._stream_key(name), fields)
        await self._redis.hincrby(self._stats_prefix + name, "total_published", 1)
        return message.id

    async def receive(self, name: str, batch_size: int = 1, timeout: float = 5.0) -> list[QueueMessage]:
        await self._ensure_group(name)
        try:
            results = await self._redis.xreadgroup(
                self._group, self._consumer,
                {self._stream_key(name): ">"},
                count=batch_size,
                block=int(timeout * 1000),
            )
        except Exception as exc:
            logger.warning("redis_queue_receive_error: %s", exc)
            return []

        messages = []
        for _stream, entries in results:
            for entry_id, fields in entries:
                now = time.time()
                delay = float(fields.get("delay_until", 0))
                if delay > now:
                    continue  # skip delayed, they'll be re-read later
                msg = QueueMessage(
                    id=fields.get("id", entry_id),
                    queue=name,
                    body=json.loads(fields.get("body", "null")),
                    headers=json.loads(fields.get("headers", "{}")),
                    priority=int(fields.get("priority", 5)),
                    timestamp=float(fields.get("timestamp", now)),
                    attempts=int(fields.get("attempts", 0)) + 1,
                    max_retries=int(fields.get("max_retries", 3)),
                    ack_id=entry_id,
                )
                messages.append(msg)
                await self._redis.hincrby(self._stats_prefix + name, "total_consumed", 1)
        return messages

    async def ack(self, name: str, message_ids: list[str]) -> int:
        if not message_ids:
            return 0
        return await self._redis.xack(self._stream_key(name), self._group, *message_ids)

    async def nack(self, name: str, message_ids: list[str], requeue: bool = True) -> int:
        count = 0
        for mid in message_ids:
            if requeue:
                # Return to pending - another consumer can pick it up
                count += 1
            else:
                # Move to dead-letter
                await self._redis.xadd(self._dlq_key(name), {"original_id": mid, "nacked_at": str(time.time())})
                await self._redis.xack(self._stream_key(name), self._group, mid)
                count += 1
        return count

    async def peek(self, name: str, count: int = 5) -> list[QueueMessage]:
        entries = await self._redis.xrange(self._stream_key(name), count=count)
        messages = []
        for entry_id, fields in entries:
            msg = QueueMessage(
                id=fields.get("id", entry_id),
                queue=name,
                body=json.loads(fields.get("body", "null")),
                headers=json.loads(fields.get("headers", "{}")),
                priority=int(fields.get("priority", 5)),
                timestamp=float(fields.get("timestamp", 0)),
                attempts=int(fields.get("attempts", 0)),
                max_retries=int(fields.get("max_retries", 3)),
                ack_id=entry_id,
            )
            messages.append(msg)
        return messages

    async def stats(self, name: str) -> QueueStats:
        depth = await self._redis.xlen(self._stream_key(name))
        dlq_depth = await self._redis.xlen(self._dlq_key(name))
        raw = await self._redis.hgetall(self._stats_prefix + name)
        return QueueStats(
            name=name,
            depth=depth,
            dead_letter_count=dlq_depth,
            total_published=int(raw.get("total_published", 0)),
            total_consumed=int(raw.get("total_consumed", 0)),
        )

    async def list_queues(self) -> list[str]:
        cursor, keys = 0, []
        while True:
            cursor, batch = await self._redis.scan(cursor, match=self._prefix + "*", count=100)
            keys.extend(k.removeprefix(self._prefix) for k in batch)
            if cursor == 0:
                break
        return sorted(set(keys))

    async def purge(self, name: str) -> int:
        depth = await self._redis.xlen(self._stream_key(name))
        await self._redis.delete(self._stream_key(name))
        await self._ensure_group(name)
        return depth

    async def dead_letter(self, name: str, count: int = 10) -> list[QueueMessage]:
        entries = await self._redis.xrange(self._dlq_key(name), count=count)
        messages = []
        for entry_id, fields in entries:
            msg = QueueMessage(
                id=fields.get("original_id", entry_id),
                queue=name,
                body=fields.get("body"),
                ack_id=entry_id,
            )
            messages.append(msg)
        return messages

    async def close(self) -> None:
        await self._redis.close()


def create_queue_backend(url: str | None = None, *, app_id: str = "default") -> QueueBackend:
    """Create a queue backend.

    - ``None`` → InMemoryQueueBackend
    - ``redis://...`` → RedisQueueBackend
    """
    if url and url.startswith(("redis://", "rediss://")):
        return RedisQueueBackend(app_id=app_id, url=url)
    return InMemoryQueueBackend()
