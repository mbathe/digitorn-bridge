"""Tests for the Digitorn event system: router, buses, stream."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digitorn.core.events.bus import FanoutEventBus, LogEventBus, NullEventBus
from digitorn.core.events.models import UniversalEvent
from digitorn.core.events.router import EventRouter, topic_matches
from digitorn.core.events.stream import ExecutionStream


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(topic: str = "digitorn.test", **kwargs) -> UniversalEvent:
    return UniversalEvent(topic=topic, source="test", **kwargs)


# ===========================================================================
# topic_matches
# ===========================================================================


class TestTopicMatches:
    """Tests for the standalone topic_matches function."""

    def test_exact_match(self):
        assert topic_matches("digitorn.module.cli.started", "digitorn.module.cli.started")

    def test_exact_no_match(self):
        assert not topic_matches("digitorn.module.cli.started", "digitorn.module.fs.started")

    def test_star_wildcard_matches_one_segment(self):
        assert topic_matches("digitorn.module.*.started", "digitorn.module.cli.started")

    def test_star_wildcard_does_not_match_multiple_segments(self):
        assert not topic_matches("digitorn.module.*.started", "digitorn.module.cli.sub.started")

    def test_hash_wildcard_matches_multiple_segments(self):
        assert topic_matches("digitorn.#", "digitorn.module.cli.action_started")

    def test_hash_wildcard_matches_single_segment(self):
        assert topic_matches("digitorn.#", "digitorn.ping")

    def test_bare_hash_matches_everything(self):
        assert topic_matches("#", "anything.at.all")

    def test_bare_hash_matches_single_segment(self):
        assert topic_matches("#", "single")

    def test_star_in_middle(self):
        assert topic_matches("a.*.c", "a.b.c")
        assert not topic_matches("a.*.c", "a.b.d")

    def test_no_match_different_depth(self):
        assert not topic_matches("a.b", "a.b.c")

    def test_no_match_shorter_topic(self):
        assert not topic_matches("a.b.c", "a.b")


# ===========================================================================
# EventRouter
# ===========================================================================


class TestEventRouter:
    """Tests for the EventRouter class."""

    def test_add_and_match(self):
        router = EventRouter()
        handler = MagicMock()
        router.add("digitorn.module.*.started", handler)
        assert handler in router.match("digitorn.module.cli.started")

    def test_match_returns_empty_when_no_handlers(self):
        router = EventRouter()
        assert router.match("digitorn.test") == []

    def test_match_returns_empty_when_no_pattern_matches(self):
        router = EventRouter()
        router.add("digitorn.module.cli.started", MagicMock())
        assert router.match("digitorn.other.topic") == []

    def test_multiple_handlers_same_pattern(self):
        router = EventRouter()
        h1, h2 = MagicMock(), MagicMock()
        router.add("a.b", h1)
        router.add("a.b", h2)
        matched = router.match("a.b")
        assert h1 in matched and h2 in matched

    def test_multiple_patterns_matching_same_topic(self):
        router = EventRouter()
        h1, h2 = MagicMock(), MagicMock()
        router.add("digitorn.#", h1)
        router.add("digitorn.module.*.started", h2)
        matched = router.match("digitorn.module.cli.started")
        assert h1 in matched and h2 in matched

    def test_remove_handler(self):
        router = EventRouter()
        handler = MagicMock()
        router.add("a.b", handler)
        router.remove("a.b", handler)
        assert router.match("a.b") == []

    def test_remove_nonexistent_handler_is_noop(self):
        router = EventRouter()
        router.remove("a.b", MagicMock())  # no error

    def test_remove_one_of_two_handlers(self):
        router = EventRouter()
        h1, h2 = MagicMock(), MagicMock()
        router.add("a.b", h1)
        router.add("a.b", h2)
        router.remove("a.b", h1)
        matched = router.match("a.b")
        assert h1 not in matched
        assert h2 in matched

    def test_clear(self):
        router = EventRouter()
        router.add("a.b", MagicMock())
        router.add("c.d", MagicMock())
        router.clear()
        assert router.match("a.b") == []
        assert router.match("c.d") == []


# ===========================================================================
# NullEventBus
# ===========================================================================


class TestNullEventBus:
    """Tests for the NullEventBus (no-op)."""

    @pytest.mark.asyncio
    async def test_publish_does_nothing(self):
        bus = NullEventBus()
        await bus.publish(_make_event())  # should not raise

    def test_subscribe_does_nothing(self):
        bus = NullEventBus()
        bus.subscribe("a.b", AsyncMock())  # should not raise

    def test_unsubscribe_does_nothing(self):
        bus = NullEventBus()
        bus.unsubscribe("a.b", AsyncMock())  # should not raise


# ===========================================================================
# LogEventBus
# ===========================================================================


class TestLogEventBus:
    """Tests for the LogEventBus."""

    @pytest.mark.asyncio
    async def test_publish_writes_ndjson(self, tmp_path: Path):
        log_file = tmp_path / "events.ndjson"
        bus = LogEventBus(log_path=log_file)
        event = _make_event("digitorn.test.write")
        await bus.publish(event)

        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["topic"] == "digitorn.test.write"

    @pytest.mark.asyncio
    async def test_publish_dispatches_to_subscribers(self, tmp_path: Path):
        log_file = tmp_path / "events.ndjson"
        bus = LogEventBus(log_path=log_file)
        handler = AsyncMock()
        bus.subscribe("digitorn.#", handler)

        event = _make_event("digitorn.hello")
        await bus.publish(event)

        handler.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_publish_does_not_dispatch_to_non_matching(self, tmp_path: Path):
        log_file = tmp_path / "events.ndjson"
        bus = LogEventBus(log_path=log_file)
        handler = AsyncMock()
        bus.subscribe("other.topic", handler)

        await bus.publish(_make_event("digitorn.hello"))
        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unsubscribe_removes_handler(self, tmp_path: Path):
        log_file = tmp_path / "events.ndjson"
        bus = LogEventBus(log_path=log_file)
        handler = AsyncMock()
        bus.subscribe("digitorn.#", handler)
        bus.unsubscribe("digitorn.#", handler)

        await bus.publish(_make_event("digitorn.hello"))
        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_publish_survives_write_error(self, tmp_path: Path):
        """A file write error should not crash publish."""
        bus = LogEventBus(log_path=tmp_path / "events.ndjson")
        with patch.object(bus, "_write_log", side_effect=OSError("disk full")):
            await bus.publish(_make_event())  # should not raise


# ===========================================================================
# FanoutEventBus
# ===========================================================================


class TestFanoutEventBus:
    """Tests for the FanoutEventBus."""

    @pytest.mark.asyncio
    async def test_publish_dispatches_to_all_backends(self):
        b1, b2 = NullEventBus(), NullEventBus()
        b1.publish = AsyncMock()
        b2.publish = AsyncMock()
        bus = FanoutEventBus([b1, b2])

        event = _make_event()
        await bus.publish(event)

        b1.publish.assert_awaited_once_with(event)
        b2.publish.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_backend_error_does_not_crash(self):
        bad = NullEventBus()
        bad.publish = AsyncMock(side_effect=RuntimeError("boom"))
        good = NullEventBus()
        good.publish = AsyncMock()

        bus = FanoutEventBus([bad, good])
        await bus.publish(_make_event())  # should not raise

        good.publish.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_add_backend(self):
        bus = FanoutEventBus()
        b = NullEventBus()
        b.publish = AsyncMock()
        bus.add_backend(b)

        await bus.publish(_make_event())
        b.publish.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_remove_backend(self):
        b = NullEventBus()
        b.publish = AsyncMock()
        bus = FanoutEventBus([b])
        bus.remove_backend(b)

        await bus.publish(_make_event())
        b.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fanout_subscribe_and_dispatch(self):
        bus = FanoutEventBus()
        handler = AsyncMock()
        bus.subscribe("digitorn.#", handler)

        event = _make_event("digitorn.ping")
        await bus.publish(event)

        handler.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_fanout_unsubscribe(self):
        bus = FanoutEventBus()
        handler = AsyncMock()
        bus.subscribe("digitorn.#", handler)
        bus.unsubscribe("digitorn.#", handler)

        await bus.publish(_make_event("digitorn.ping"))
        handler.assert_not_awaited()


# ===========================================================================
# ExecutionStream
# ===========================================================================


class TestExecutionStream:
    """Tests for the ExecutionStream."""

    def _make_stream(self) -> tuple[ExecutionStream, AsyncMock]:
        bus = AsyncMock()
        parent = _make_event(
            topic="digitorn.module.fs.action_started",
            app_id="app1",
            session_id="sess1",
            correlation_id="corr1",
        )
        stream = ExecutionStream(
            event_bus=bus,
            parent_event=parent,
            module_id="fs",
            action="read_file",
        )
        return stream, bus

    @pytest.mark.asyncio
    async def test_emit_publishes_to_bus(self):
        stream, bus = self._make_stream()
        await stream.progress(1, 10, "reading")
        bus.publish.assert_awaited_once()
        published_event = bus.publish.call_args[0][0]
        assert published_event.topic == "digitorn.module.fs.action_progress"
        assert published_event.data["step"] == 1
        assert published_event.data["total"] == 10

    @pytest.mark.asyncio
    async def test_emit_catches_exception(self):
        stream, bus = self._make_stream()
        bus.publish.side_effect = RuntimeError("bus down")
        await stream.progress(1, 5)  # should not raise

    @pytest.mark.asyncio
    async def test_partial_result(self):
        stream, bus = self._make_stream()
        await stream.partial_result({"rows": 42})
        published = bus.publish.call_args[0][0]
        assert published.topic == "digitorn.module.fs.action_partial_result"
        assert published.data["result"] == {"rows": 42}

    @pytest.mark.asyncio
    async def test_log_event(self):
        stream, bus = self._make_stream()
        await stream.log("info", "something happened", {"key": "val"})
        published = bus.publish.call_args[0][0]
        assert published.topic == "digitorn.module.fs.action_log"
        assert published.data["level"] == "info"
        assert published.data["message"] == "something happened"
        assert published.data["details"] == {"key": "val"}

    @pytest.mark.asyncio
    async def test_warning_delegates_to_log(self):
        stream, bus = self._make_stream()
        await stream.warning("watch out")
        published = bus.publish.call_args[0][0]
        assert published.data["level"] == "warning"
        assert published.data["message"] == "watch out"

    @pytest.mark.asyncio
    async def test_child_event_inherits_causality(self):
        stream, bus = self._make_stream()
        await stream.progress(2, 5)
        child = bus.publish.call_args[0][0]
        assert child.correlation_id == "corr1"
        assert child.app_id == "app1"
        assert child.session_id == "sess1"


# ===========================================================================
# UniversalEvent model
# ===========================================================================


class TestUniversalEvent:
    """Basic tests for the UniversalEvent model."""

    def test_child_inherits_scope(self):
        parent = _make_event(
            topic="digitorn.parent",
            app_id="app1",
            session_id="sess1",
            agent_id="agent1",
            correlation_id="corr1",
        )
        child = parent.child(topic="digitorn.child", event_type="progress")
        assert child.correlation_id == "corr1"
        assert child.causation_id == parent.event_id
        assert child.parent_id == parent.event_id
        assert child.app_id == "app1"
        assert child.session_id == "sess1"
        assert child.agent_id == "agent1"

    def test_child_uses_event_id_as_correlation_if_none(self):
        parent = _make_event(topic="digitorn.parent", correlation_id=None)
        child = parent.child(topic="digitorn.child")
        assert child.correlation_id == parent.event_id

    def test_sibling_shares_parent(self):
        parent = _make_event(topic="digitorn.parent", correlation_id="corr1")
        child = parent.child(topic="digitorn.child")
        sibling = child.sibling(topic="digitorn.sibling")
        assert sibling.parent_id == child.parent_id
        assert sibling.correlation_id == child.correlation_id
