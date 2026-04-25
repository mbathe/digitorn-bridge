"""Tests for the Queue module — InMemoryQueueBackend and QueueModule actions.

Covers:
    - InMemoryQueueBackend: create, publish, receive, priority ordering, ack,
      nack, nack-with-requeue, dead-letter after max retries, peek, stats,
      list_queues, purge, delete
    - QueueModule: all actions via direct instantiation (in-memory backend)
"""

from __future__ import annotations

import asyncio
import time

import pytest

from digitorn.modules.queue.backends import InMemoryQueueBackend, QueueStats, create_queue_backend
from digitorn.modules.queue.message import QueueMessage
from digitorn.modules.queue.module import QueueModule
from digitorn.modules.queue.params import (
    AckParams,
    CreateQueueParams,
    DeadLetterParams,
    DeleteQueueParams,
    ListQueuesParams,
    NackParams,
    PeekParams,
    PublishParams,
    PurgeParams,
    QueueStatsParams,
    ReceiveParams,
)


# ═══════════════════════════════════════════════════════════════════════
# InMemoryQueueBackend — Unit Tests
# ═══════════════════════════════════════════════════════════════════════


class TestInMemoryQueueBackend:
    """Test the in-memory queue backend directly."""

    @pytest.fixture
    def backend(self) -> InMemoryQueueBackend:
        return InMemoryQueueBackend()

    @pytest.mark.asyncio
    async def test_create_queue(self, backend: InMemoryQueueBackend) -> None:
        await backend.create_queue("test-q")
        queues = await backend.list_queues()
        assert "test-q" in queues

    @pytest.mark.asyncio
    async def test_create_queue_idempotent(self, backend: InMemoryQueueBackend) -> None:
        await backend.create_queue("test-q")
        await backend.create_queue("test-q")
        queues = await backend.list_queues()
        assert queues.count("test-q") == 1

    @pytest.mark.asyncio
    async def test_publish_and_receive_single(self, backend: InMemoryQueueBackend) -> None:
        await backend.create_queue("q1")
        msg = QueueMessage(queue="q1", body={"text": "hello"})
        msg_id = await backend.publish("q1", msg)
        assert msg_id == msg.id

        received = await backend.receive("q1", batch_size=1, timeout=1.0)
        assert len(received) == 1
        assert received[0].body == {"text": "hello"}
        assert received[0].ack_id is not None
        assert received[0].attempts == 1

    @pytest.mark.asyncio
    async def test_priority_ordering(self, backend: InMemoryQueueBackend) -> None:
        """Publish 3 messages with different priorities; receive in priority order."""
        await backend.create_queue("pq")
        # priority 0 = highest, 9 = lowest
        low = QueueMessage(queue="pq", body="low", priority=9)
        mid = QueueMessage(queue="pq", body="mid", priority=5)
        high = QueueMessage(queue="pq", body="high", priority=1)

        await backend.publish("pq", low)
        await backend.publish("pq", mid)
        await backend.publish("pq", high)

        msgs = await backend.receive("pq", batch_size=3, timeout=1.0)
        assert len(msgs) == 3
        assert msgs[0].body == "high"
        assert msgs[1].body == "mid"
        assert msgs[2].body == "low"

    @pytest.mark.asyncio
    async def test_ack_removes_from_unacked(self, backend: InMemoryQueueBackend) -> None:
        await backend.create_queue("q")
        msg = QueueMessage(queue="q", body="x")
        await backend.publish("q", msg)
        received = await backend.receive("q", batch_size=1, timeout=1.0)
        ack_id = received[0].ack_id

        count = await backend.ack("q", [ack_id])
        assert count == 1
        # Acking again should return 0
        count = await backend.ack("q", [ack_id])
        assert count == 0

    @pytest.mark.asyncio
    async def test_nack_with_requeue(self, backend: InMemoryQueueBackend) -> None:
        """Nack with requeue=True puts the message back in the queue."""
        await backend.create_queue("q")
        msg = QueueMessage(queue="q", body="retry-me", max_retries=3)
        await backend.publish("q", msg)

        received = await backend.receive("q", batch_size=1, timeout=1.0)
        ack_id = received[0].ack_id
        assert received[0].attempts == 1

        count = await backend.nack("q", [ack_id], requeue=True)
        assert count == 1

        # Message should be available again
        received2 = await backend.receive("q", batch_size=1, timeout=1.0)
        assert len(received2) == 1
        assert received2[0].body == "retry-me"
        assert received2[0].attempts == 2

    @pytest.mark.asyncio
    async def test_nack_without_requeue_goes_to_dlq(self, backend: InMemoryQueueBackend) -> None:
        """Nack with requeue=False sends message to dead-letter queue."""
        await backend.create_queue("q")
        msg = QueueMessage(queue="q", body="dead", max_retries=3)
        await backend.publish("q", msg)

        received = await backend.receive("q", batch_size=1, timeout=1.0)
        count = await backend.nack("q", [received[0].ack_id], requeue=False)
        assert count == 1

        dlq = await backend.dead_letter("q")
        assert len(dlq) == 1
        assert dlq[0].body == "dead"

    @pytest.mark.asyncio
    async def test_dead_letter_after_max_retries(self, backend: InMemoryQueueBackend) -> None:
        """Message goes to DLQ when attempts reach max_retries on nack with requeue=True."""
        await backend.create_queue("q")
        msg = QueueMessage(queue="q", body="fail", max_retries=2)
        await backend.publish("q", msg)

        # First receive: attempts=1, nack requeue -> back in queue (1 < 2)
        r1 = await backend.receive("q", batch_size=1, timeout=1.0)
        assert r1[0].attempts == 1
        await backend.nack("q", [r1[0].ack_id], requeue=True)

        # Second receive: attempts=2, nack requeue -> DLQ (2 >= 2)
        r2 = await backend.receive("q", batch_size=1, timeout=1.0)
        assert r2[0].attempts == 2
        await backend.nack("q", [r2[0].ack_id], requeue=True)

        # Queue should be empty, DLQ should have the message
        r3 = await backend.receive("q", batch_size=1, timeout=0.1)
        assert len(r3) == 0

        dlq = await backend.dead_letter("q")
        assert len(dlq) == 1
        assert dlq[0].body == "fail"

    @pytest.mark.asyncio
    async def test_peek_without_consuming(self, backend: InMemoryQueueBackend) -> None:
        await backend.create_queue("q")
        msg = QueueMessage(queue="q", body="peek-me")
        await backend.publish("q", msg)

        peeked = await backend.peek("q", count=5)
        assert len(peeked) == 1
        assert peeked[0].body == "peek-me"

        # Message should still be available for receive
        received = await backend.receive("q", batch_size=1, timeout=1.0)
        assert len(received) == 1
        assert received[0].body == "peek-me"

    @pytest.mark.asyncio
    async def test_queue_stats(self, backend: InMemoryQueueBackend) -> None:
        await backend.create_queue("q")
        msg1 = QueueMessage(queue="q", body="a")
        msg2 = QueueMessage(queue="q", body="b")
        await backend.publish("q", msg1)
        await backend.publish("q", msg2)

        stats = await backend.stats("q")
        assert stats.name == "q"
        assert stats.depth == 2
        assert stats.total_published == 2
        assert stats.total_consumed == 0
        assert stats.dead_letter_count == 0

        # Consume one
        received = await backend.receive("q", batch_size=1, timeout=1.0)
        assert len(received) == 1

        stats2 = await backend.stats("q")
        assert stats2.depth == 1
        assert stats2.total_consumed == 1

    @pytest.mark.asyncio
    async def test_stats_unknown_queue(self, backend: InMemoryQueueBackend) -> None:
        stats = await backend.stats("nonexistent")
        assert stats.name == "nonexistent"
        assert stats.depth == 0

    @pytest.mark.asyncio
    async def test_list_queues(self, backend: InMemoryQueueBackend) -> None:
        await backend.create_queue("alpha")
        await backend.create_queue("beta")
        await backend.create_queue("gamma")
        queues = await backend.list_queues()
        assert queues == ["alpha", "beta", "gamma"]

    @pytest.mark.asyncio
    async def test_purge(self, backend: InMemoryQueueBackend) -> None:
        await backend.create_queue("q")
        await backend.publish("q", QueueMessage(queue="q", body="a"))
        await backend.publish("q", QueueMessage(queue="q", body="b"))

        count = await backend.purge("q")
        assert count == 2

        stats = await backend.stats("q")
        assert stats.depth == 0

        # Queue still exists after purge
        queues = await backend.list_queues()
        assert "q" in queues

    @pytest.mark.asyncio
    async def test_purge_nonexistent(self, backend: InMemoryQueueBackend) -> None:
        count = await backend.purge("nope")
        assert count == 0

    @pytest.mark.asyncio
    async def test_delete_queue(self, backend: InMemoryQueueBackend) -> None:
        await backend.create_queue("q")
        deleted = await backend.delete_queue("q")
        assert deleted is True

        queues = await backend.list_queues()
        assert "q" not in queues

    @pytest.mark.asyncio
    async def test_delete_nonexistent_queue(self, backend: InMemoryQueueBackend) -> None:
        deleted = await backend.delete_queue("nope")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_receive_empty_queue_timeout(self, backend: InMemoryQueueBackend) -> None:
        """Receive on an empty queue returns empty list after timeout."""
        await backend.create_queue("q")
        start = time.monotonic()
        msgs = await backend.receive("q", batch_size=1, timeout=0.2)
        elapsed = time.monotonic() - start
        assert len(msgs) == 0
        assert elapsed >= 0.15  # should have waited close to timeout

    @pytest.mark.asyncio
    async def test_receive_from_nonexistent_queue(self, backend: InMemoryQueueBackend) -> None:
        msgs = await backend.receive("nope", batch_size=1, timeout=0.1)
        assert msgs == []

    @pytest.mark.asyncio
    async def test_publish_auto_creates_queue(self, backend: InMemoryQueueBackend) -> None:
        """Publishing to a non-existent queue should auto-create it."""
        msg = QueueMessage(queue="auto-q", body="auto")
        await backend.publish("auto-q", msg)
        queues = await backend.list_queues()
        assert "auto-q" in queues

    @pytest.mark.asyncio
    async def test_delayed_message(self, backend: InMemoryQueueBackend) -> None:
        """Delayed messages are not visible until delay expires."""
        await backend.create_queue("dq")
        msg = QueueMessage(queue="dq", body="delayed", delay_until=time.time() + 0.3)
        await backend.publish("dq", msg)

        # Should not be visible yet
        received = await backend.receive("dq", batch_size=1, timeout=0.1)
        assert len(received) == 0

        # Wait for delay to expire
        await asyncio.sleep(0.35)

        received2 = await backend.receive("dq", batch_size=1, timeout=0.5)
        assert len(received2) == 1
        assert received2[0].body == "delayed"

    @pytest.mark.asyncio
    async def test_close_clears_state(self, backend: InMemoryQueueBackend) -> None:
        await backend.create_queue("q")
        await backend.publish("q", QueueMessage(queue="q", body="x"))
        await backend.close()
        # Internal state is cleared
        assert len(backend._queues) == 0

    @pytest.mark.asyncio
    async def test_dead_letter_count_limit(self, backend: InMemoryQueueBackend) -> None:
        """dead_letter() respects count parameter."""
        await backend.create_queue("q")
        for i in range(5):
            msg = QueueMessage(queue="q", body=f"fail-{i}", max_retries=0)
            await backend.publish("q", msg)
            r = await backend.receive("q", batch_size=1, timeout=0.5)
            await backend.nack("q", [r[0].ack_id], requeue=True)

        dlq = await backend.dead_letter("q", count=3)
        assert len(dlq) == 3


# ═══════════════════════════════════════════════════════════════════════
# create_queue_backend factory
# ═══════════════════════════════════════════════════════════════════════


class TestCreateQueueBackend:

    def test_none_returns_inmemory(self) -> None:
        b = create_queue_backend(url=None)
        assert isinstance(b, InMemoryQueueBackend)

    def test_empty_string_returns_inmemory(self) -> None:
        b = create_queue_backend(url="")
        assert isinstance(b, InMemoryQueueBackend)


# ═══════════════════════════════════════════════════════════════════════
# QueueMessage — Unit Tests
# ═══════════════════════════════════════════════════════════════════════


class TestQueueMessage:

    def test_to_dict_roundtrip(self) -> None:
        msg = QueueMessage(queue="q", body={"key": "val"}, priority=2, headers={"h": "v"})
        d = msg.to_dict()
        restored = QueueMessage.from_dict(d)
        assert restored.queue == "q"
        assert restored.body == {"key": "val"}
        assert restored.priority == 2
        assert restored.headers == {"h": "v"}

    def test_is_delayed_future(self) -> None:
        msg = QueueMessage(delay_until=time.time() + 100)
        assert msg.is_delayed() is True

    def test_is_delayed_past(self) -> None:
        msg = QueueMessage(delay_until=time.time() - 1)
        assert msg.is_delayed() is False

    def test_is_delayed_none(self) -> None:
        msg = QueueMessage()
        assert msg.is_delayed() is False


# ═══════════════════════════════════════════════════════════════════════
# QueueStats
# ═══════════════════════════════════════════════════════════════════════


class TestQueueStats:

    def test_to_dict(self) -> None:
        s = QueueStats(name="q", depth=10, dead_letter_count=2, total_published=50, total_consumed=48)
        d = s.to_dict()
        assert d["name"] == "q"
        assert d["depth"] == 10
        assert d["dead_letter_count"] == 2
        assert d["total_published"] == 50
        assert d["total_consumed"] == 48
        assert d["consumer_count"] == 0


# ═══════════════════════════════════════════════════════════════════════
# QueueModule — Action Tests
# ═══════════════════════════════════════════════════════════════════════


class TestQueueModule:
    """Test QueueModule actions end-to-end with in-memory backend."""

    @pytest.fixture
    async def mod(self) -> QueueModule:
        m = QueueModule()
        m._config = {}
        await m.on_start()
        yield m
        await m.on_stop()

    @pytest.mark.asyncio
    async def test_create_queue_action(self, mod: QueueModule) -> None:
        result = await mod.create_queue(CreateQueueParams(name="test-q"))
        assert result.success is True
        assert result.data["queue"] == "test-q"
        assert result.data["created"] is True

    @pytest.mark.asyncio
    async def test_publish_action(self, mod: QueueModule) -> None:
        await mod.create_queue(CreateQueueParams(name="q"))
        result = await mod.publish(PublishParams(queue="q", message={"foo": "bar"}, priority=3))
        assert result.success is True
        assert "message_id" in result.data
        assert result.data["queue"] == "q"
        assert result.data["priority"] == 3
        assert result.data["delayed"] is False

    @pytest.mark.asyncio
    async def test_receive_action_manual_ack(self, mod: QueueModule) -> None:
        await mod.create_queue(CreateQueueParams(name="q"))
        await mod.publish(PublishParams(queue="q", message="hello"))
        result = await mod.receive(ReceiveParams(queue="q", timeout=1.0, ack_mode="manual"))
        assert result.success is True
        assert result.data["count"] == 1
        assert result.data["messages"][0]["body"] == "hello"
        assert result.data["messages"][0]["ack_id"] is not None

    @pytest.mark.asyncio
    async def test_receive_action_auto_ack(self, mod: QueueModule) -> None:
        await mod.create_queue(CreateQueueParams(name="q"))
        await mod.publish(PublishParams(queue="q", message="auto"))
        result = await mod.receive(ReceiveParams(queue="q", timeout=1.0, ack_mode="auto"))
        assert result.success is True
        assert result.data["count"] == 1
        # Auto-ack means a second ack should return 0
        ack_id = result.data["messages"][0]["ack_id"]
        ack_result = await mod.ack(AckParams(queue="q", message_ids=[ack_id]))
        assert ack_result.data["acknowledged"] == 0

    @pytest.mark.asyncio
    async def test_receive_action_empty_timeout(self, mod: QueueModule) -> None:
        await mod.create_queue(CreateQueueParams(name="q"))
        result = await mod.receive(ReceiveParams(queue="q", timeout=0.2))
        assert result.success is True
        assert result.data["count"] == 0

    @pytest.mark.asyncio
    async def test_ack_action(self, mod: QueueModule) -> None:
        await mod.create_queue(CreateQueueParams(name="q"))
        await mod.publish(PublishParams(queue="q", message="x"))
        recv = await mod.receive(ReceiveParams(queue="q", timeout=1.0))
        ack_id = recv.data["messages"][0]["ack_id"]

        result = await mod.ack(AckParams(queue="q", message_ids=[ack_id]))
        assert result.success is True
        assert result.data["acknowledged"] == 1

    @pytest.mark.asyncio
    async def test_nack_action_requeue(self, mod: QueueModule) -> None:
        await mod.create_queue(CreateQueueParams(name="q"))
        await mod.publish(PublishParams(queue="q", message="retry"))
        recv = await mod.receive(ReceiveParams(queue="q", timeout=1.0))
        ack_id = recv.data["messages"][0]["ack_id"]

        result = await mod.nack(NackParams(queue="q", message_ids=[ack_id], requeue=True))
        assert result.success is True
        assert result.data["rejected"] == 1
        assert result.data["requeued"] is True

        # Should be receivable again
        recv2 = await mod.receive(ReceiveParams(queue="q", timeout=1.0))
        assert recv2.data["count"] == 1

    @pytest.mark.asyncio
    async def test_nack_action_no_requeue(self, mod: QueueModule) -> None:
        await mod.create_queue(CreateQueueParams(name="q"))
        await mod.publish(PublishParams(queue="q", message="dead"))
        recv = await mod.receive(ReceiveParams(queue="q", timeout=1.0))
        ack_id = recv.data["messages"][0]["ack_id"]

        result = await mod.nack(NackParams(queue="q", message_ids=[ack_id], requeue=False))
        assert result.success is True
        assert result.data["requeued"] is False

        # Should be in DLQ
        dlq = await mod.dead_letter(DeadLetterParams(queue="q"))
        assert dlq.data["count"] == 1

    @pytest.mark.asyncio
    async def test_peek_action(self, mod: QueueModule) -> None:
        await mod.create_queue(CreateQueueParams(name="q"))
        await mod.publish(PublishParams(queue="q", message="peek"))
        result = await mod.peek(PeekParams(queue="q", count=5))
        assert result.success is True
        assert result.data["count"] == 1
        assert result.data["messages"][0]["body"] == "peek"

        # Message should still be there
        recv = await mod.receive(ReceiveParams(queue="q", timeout=1.0))
        assert recv.data["count"] == 1

    @pytest.mark.asyncio
    async def test_queue_stats_action(self, mod: QueueModule) -> None:
        await mod.create_queue(CreateQueueParams(name="q"))
        await mod.publish(PublishParams(queue="q", message="a"))
        await mod.publish(PublishParams(queue="q", message="b"))

        result = await mod.queue_stats(QueueStatsParams(queue="q"))
        assert result.success is True
        assert result.data["name"] == "q"
        assert result.data["depth"] == 2
        assert result.data["total_published"] == 2

    @pytest.mark.asyncio
    async def test_list_queues_action(self, mod: QueueModule) -> None:
        await mod.create_queue(CreateQueueParams(name="alpha"))
        await mod.create_queue(CreateQueueParams(name="beta"))

        result = await mod.list_queues(ListQueuesParams())
        assert result.success is True
        assert "alpha" in result.data["queues"]
        assert "beta" in result.data["queues"]
        assert result.data["count"] == 2

    @pytest.mark.asyncio
    async def test_delete_queue_action(self, mod: QueueModule) -> None:
        await mod.create_queue(CreateQueueParams(name="q"))
        result = await mod.delete_queue(DeleteQueueParams(queue="q"))
        assert result.success is True
        assert result.data["deleted"] is True

        listing = await mod.list_queues(ListQueuesParams())
        assert "q" not in listing.data["queues"]

    @pytest.mark.asyncio
    async def test_purge_action(self, mod: QueueModule) -> None:
        await mod.create_queue(CreateQueueParams(name="q"))
        await mod.publish(PublishParams(queue="q", message="a"))
        await mod.publish(PublishParams(queue="q", message="b"))

        result = await mod.purge(PurgeParams(queue="q"))
        assert result.success is True
        assert result.data["purged"] == 2

        stats = await mod.queue_stats(QueueStatsParams(queue="q"))
        assert stats.data["depth"] == 0

    @pytest.mark.asyncio
    async def test_dead_letter_action(self, mod: QueueModule) -> None:
        await mod.create_queue(CreateQueueParams(name="q"))
        await mod.publish(PublishParams(queue="q", message="will-fail"))

        recv = await mod.receive(ReceiveParams(queue="q", timeout=1.0))
        ack_id = recv.data["messages"][0]["ack_id"]
        await mod.nack(NackParams(queue="q", message_ids=[ack_id], requeue=False))

        result = await mod.dead_letter(DeadLetterParams(queue="q", count=10))
        assert result.success is True
        assert result.data["count"] == 1
        assert result.data["messages"][0]["body"] == "will-fail"

    @pytest.mark.asyncio
    async def test_publish_priority_ordering_via_module(self, mod: QueueModule) -> None:
        """Publish via module actions with different priorities, verify receive order."""
        await mod.create_queue(CreateQueueParams(name="pq"))
        await mod.publish(PublishParams(queue="pq", message="low", priority=9))
        await mod.publish(PublishParams(queue="pq", message="high", priority=1))
        await mod.publish(PublishParams(queue="pq", message="mid", priority=5))

        result = await mod.receive(ReceiveParams(queue="pq", timeout=1.0, batch_size=3))
        bodies = [m["body"] for m in result.data["messages"]]
        assert bodies == ["high", "mid", "low"]

    @pytest.mark.asyncio
    async def test_publish_with_delay_via_module(self, mod: QueueModule) -> None:
        """Publish with delay_seconds; message should not be available immediately."""
        await mod.create_queue(CreateQueueParams(name="dq"))
        result = await mod.publish(PublishParams(queue="dq", message="later", delay_seconds=0.3))
        assert result.data["delayed"] is True

        # Not available yet
        recv = await mod.receive(ReceiveParams(queue="dq", timeout=0.1))
        assert recv.data["count"] == 0

        # Wait for delay to pass
        await asyncio.sleep(0.35)

        recv2 = await mod.receive(ReceiveParams(queue="dq", timeout=0.5))
        assert recv2.data["count"] == 1
        assert recv2.data["messages"][0]["body"] == "later"

    @pytest.mark.asyncio
    async def test_publish_with_headers(self, mod: QueueModule) -> None:
        await mod.create_queue(CreateQueueParams(name="q"))
        await mod.publish(PublishParams(
            queue="q", message="tagged", headers={"env": "test", "source": "ci"},
        ))
        recv = await mod.receive(ReceiveParams(queue="q", timeout=1.0))
        assert recv.data["messages"][0]["headers"]["env"] == "test"
        assert recv.data["messages"][0]["headers"]["source"] == "ci"

    @pytest.mark.asyncio
    async def test_module_on_stop_clears_backend(self, mod: QueueModule) -> None:
        await mod.create_queue(CreateQueueParams(name="q"))
        await mod.on_stop()
        assert mod._backend is None

    @pytest.mark.asyncio
    async def test_ensure_backend_fallback(self) -> None:
        """_ensure_backend creates InMemoryQueueBackend if none configured."""
        m = QueueModule()
        # Don't call on_start — backend is None
        backend = m._ensure_backend()
        assert isinstance(backend, InMemoryQueueBackend)

    @pytest.mark.asyncio
    async def test_state_snapshot_and_restore(self, mod: QueueModule) -> None:
        snap = mod.state_snapshot()
        assert snap["app_id"] == "default"
        assert snap["active_subscriptions"] == []

        await mod.restore_state({"app_id": "my-app"})
        assert mod._app_id == "my-app"

    @pytest.mark.asyncio
    async def test_get_manifest(self, mod: QueueModule) -> None:
        manifest = mod.get_manifest()
        assert manifest.module_id == "queue"
        assert "queue" in manifest.tags
