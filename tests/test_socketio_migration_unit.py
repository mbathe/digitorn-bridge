"""Unit tests for the Socket.IO session event migration.

Covers the pure-logic pieces that don't need a running server:

- ``EventBuffer``   - monotonic seq, per-user isolation, ring
                      eviction, filtered replay.
- ``SocketIOBus``   - key parsing, room routing, approval fanout,
                      in-process handler dispatch, event kind map.
- ``InboxProducer`` - envelope → inbox row mapping for every
                      supported event type.
- ``ApprovalQueue`` - new ``app_id`` / ``session_id`` fields,
                      callback registration, and the migration's
                      bus-publish callback end-to-end.

Integration tests that need a real uvicorn + Socket.IO client live
in ``test_socketio_migration_integration.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from digitorn.core.events.event_buffer import EventBuffer
from digitorn.core.events.session_bus import SocketIOBus, _EVENT_KIND_MAP
from digitorn.core.inbox.kinds import InboxKind
from digitorn.core.inbox.producer import InboxProducer
from digitorn.core.runtime.approval import ApprovalQueue, ApprovalRequest


# ──────────────────────────────────────────────────────────────────
# Fakes
# ──────────────────────────────────────────────────────────────────


class FakeSio:
    """In-memory stand-in for ``socketio.AsyncServer``.

    Records every ``emit()`` call as ``(room, envelope)`` so tests
    can assert on routing.
    """

    def __init__(self) -> None:
        self.emits: list[tuple[str, dict[str, Any]]] = []
        self.raise_on_emit = False

    async def emit(
        self, event: str, data: dict[str, Any],
        room: str | None = None, namespace: str | None = None,
    ) -> None:
        if self.raise_on_emit:
            raise RuntimeError("simulated socket error")
        assert event == "event"
        assert namespace == "/events"
        self.emits.append((room or "", data))

    def rooms_with_type(self, raw_type: str) -> list[str]:
        return [r for r, env in self.emits if env["type"] == raw_type]


class FakeInboxStore:
    """Record what ``create_item`` is called with."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.raise_on_create = False

    async def create_item(self, **fields: Any) -> dict[str, Any]:
        if self.raise_on_create:
            raise RuntimeError("store down")
        item = dict(fields)
        item.setdefault("id", f"item-{len(self.items)+1}")
        self.items.append(item)
        return item


# ══════════════════════════════════════════════════════════════════
# EventBuffer
# ══════════════════════════════════════════════════════════════════


class TestEventBuffer:

    def test_next_seq_monotonic_per_user(self):
        buf = EventBuffer()
        assert buf.next_seq("alice") == 1
        assert buf.next_seq("alice") == 2
        assert buf.next_seq("alice") == 3
        assert buf.next_seq("bob") == 1  # independent counter
        assert buf.next_seq("bob") == 2
        assert buf.next_seq("alice") == 4

    def test_get_latest_seq_empty_user(self):
        buf = EventBuffer()
        assert buf.get_latest_seq("nobody") == 0

    def test_append_returns_envelope_with_seq_and_ts(self):
        buf = EventBuffer()
        env = buf.append(
            user_id="alice", type="token", kind="session",
            payload={"text": "hi"}, app_id="a1", session_id="s1",
        )
        assert env["seq"] == 1
        assert env["type"] == "token"
        assert env["kind"] == "session"
        assert env["app_id"] == "a1"
        assert env["session_id"] == "s1"
        assert env["payload"] == {"text": "hi"}
        assert isinstance(env["ts"], str) and "T" in env["ts"]

    def test_append_none_payload_normalizes_to_empty_dict(self):
        buf = EventBuffer()
        env = buf.append(
            user_id="alice", type="ping", kind="session", payload=None,
        )
        assert env["payload"] == {}

    def test_replay_filters_by_seq(self):
        buf = EventBuffer()
        for i in range(5):
            buf.append(
                user_id="alice", type=f"e{i}", kind="session",
                payload={"i": i},
            )
        out = buf.replay("alice", since=2)
        assert len(out) == 3
        assert [e["type"] for e in out] == ["e2", "e3", "e4"]
        # "since" is exclusive - seq>since
        assert out[0]["seq"] == 3

    def test_replay_filters_by_session_id(self):
        buf = EventBuffer()
        buf.append(user_id="a", type="x", kind="session",
                   payload={}, session_id="s1")
        buf.append(user_id="a", type="y", kind="session",
                   payload={}, session_id="s2")
        buf.append(user_id="a", type="z", kind="session",
                   payload={}, session_id="s1")
        out = buf.replay("a", since=0, session_id="s1")
        assert [e["type"] for e in out] == ["x", "z"]

    def test_replay_filters_by_app_id(self):
        buf = EventBuffer()
        buf.append(user_id="a", type="x", kind="session", payload={}, app_id="app1")
        buf.append(user_id="a", type="y", kind="session", payload={}, app_id="app2")
        out = buf.replay("a", since=0, app_id="app1")
        assert len(out) == 1 and out[0]["type"] == "x"

    def test_replay_empty_for_unknown_user(self):
        buf = EventBuffer()
        assert buf.replay("ghost", since=0) == []

    def test_replay_limit_caps_result(self):
        buf = EventBuffer()
        for i in range(10):
            buf.append(user_id="a", type=f"e{i}", kind="session", payload={})
        out = buf.replay("a", since=0, limit=3)
        assert len(out) == 3

    def test_ring_eviction(self):
        """Older events are evicted when the buffer exceeds max."""
        buf = EventBuffer(max_per_user=3)
        for i in range(5):
            buf.append(user_id="a", type=f"e{i}", kind="session", payload={})
        # Only last 3 kept
        out = buf.replay("a", since=0)
        assert len(out) == 3
        assert [e["type"] for e in out] == ["e2", "e3", "e4"]
        # But seq counter keeps going even after eviction
        assert buf.get_latest_seq("a") == 5

    def test_per_user_isolation(self):
        buf = EventBuffer()
        buf.append(user_id="alice", type="a", kind="session", payload={})
        buf.append(user_id="bob", type="b", kind="session", payload={})
        alice = buf.replay("alice", since=0)
        bob = buf.replay("bob", since=0)
        assert [e["type"] for e in alice] == ["a"]
        assert [e["type"] for e in bob] == ["b"]

    def test_clear_user_resets_seq(self):
        buf = EventBuffer()
        buf.append(user_id="a", type="x", kind="session", payload={})
        buf.append(user_id="a", type="y", kind="session", payload={})
        buf.clear_user("a")
        assert buf.get_latest_seq("a") == 0
        assert buf.replay("a", since=0) == []
        # Next append starts from 1 again
        env = buf.append(user_id="a", type="z", kind="session", payload={})
        assert env["seq"] == 1


# ══════════════════════════════════════════════════════════════════
# SocketIOBus - key parsing
# ══════════════════════════════════════════════════════════════════


class TestSocketIOBusKeys:

    def test_session_key_format(self):
        assert SocketIOBus.session_key("app1", "sess1", "alice") == "app1:alice:sess1"

    def test_session_key_default_user(self):
        assert SocketIOBus.session_key("app1", "sess1") == "app1:local:sess1"

    def test_user_key_format(self):
        assert SocketIOBus.user_key("alice") == "user:alice"

    def test_user_key_empty_falls_back_to_local(self):
        assert SocketIOBus.user_key("") == "user:local"

    def test_parse_session_key(self):
        bus = SocketIOBus(sio=FakeSio())
        assert bus._parse_key("app1:alice:sess1") == ("app1", "alice", "sess1")

    def test_parse_user_key(self):
        bus = SocketIOBus(sio=FakeSio())
        assert bus._parse_key("user:alice") == (None, "alice", None)

    def test_parse_user_key_empty_falls_back_to_local(self):
        bus = SocketIOBus(sio=FakeSio())
        assert bus._parse_key("user:") == (None, "local", None)

    def test_parse_malformed_key(self):
        bus = SocketIOBus(sio=FakeSio())
        assert bus._parse_key("malformed") == (None, "local", None)


# ══════════════════════════════════════════════════════════════════
# SocketIOBus - publish routing + fanout
# ══════════════════════════════════════════════════════════════════


class TestSocketIOBusPublish:

    async def test_publish_session_event_routes_to_session_room(self):
        sio = FakeSio()
        bus = SocketIOBus(sio=sio)
        key = bus.session_key("myapp", "sess1", "alice")
        result = await bus.publish(key, {"type": "token", "data": {"text": "hi"}})
        assert result == 1
        assert len(sio.emits) == 1
        room, env = sio.emits[0]
        assert room == "session:sess1"
        assert env["type"] == "token"
        assert env["kind"] == "session"
        assert env["session_id"] == "sess1"
        assert env["app_id"] == "myapp"
        assert env["payload"] == {"text": "hi"}
        assert env["seq"] == 1

    async def test_publish_user_event_routes_to_user_room(self):
        sio = FakeSio()
        bus = SocketIOBus(sio=sio)
        await bus.publish(bus.user_key("alice"), {"type": "notification", "data": {"msg": "x"}})
        assert sio.emits[0][0] == "user:alice"

    async def test_publish_with_dict_payload_preserved(self):
        sio = FakeSio()
        bus = SocketIOBus(sio=sio)
        await bus.publish(
            bus.session_key("a", "s", "u"),
            {"type": "tool_call", "data": {"name": "Bash", "args": {"cmd": "ls"}}},
        )
        assert sio.emits[0][1]["payload"] == {"name": "Bash", "args": {"cmd": "ls"}}

    async def test_publish_with_non_dict_data_wraps_it(self):
        sio = FakeSio()
        bus = SocketIOBus(sio=sio)
        await bus.publish(
            bus.session_key("a", "s", "u"),
            {"type": "token", "data": "raw string"},
        )
        assert sio.emits[0][1]["payload"] == {"data": "raw string"}

    async def test_publish_with_no_data_empty_payload(self):
        sio = FakeSio()
        bus = SocketIOBus(sio=sio)
        await bus.publish(bus.session_key("a", "s", "u"), {"type": "ping"})
        assert sio.emits[0][1]["payload"] == {}

    async def test_publish_seq_monotonic_across_calls(self):
        sio = FakeSio()
        bus = SocketIOBus(sio=sio)
        k = bus.session_key("a", "s", "u")
        await bus.publish(k, {"type": "token"})
        await bus.publish(k, {"type": "token"})
        await bus.publish(k, {"type": "result"})
        seqs = [env["seq"] for _, env in sio.emits]
        assert seqs == [1, 2, 3]

    async def test_approval_request_fans_out_to_session_and_user(self):
        sio = FakeSio()
        bus = SocketIOBus(sio=sio)
        key = bus.session_key("myapp", "sess1", "alice")
        await bus.publish(key, {"type": "approval_request", "data": {"tool": "Bash"}})
        rooms = [r for r, _ in sio.emits]
        assert "session:sess1" in rooms
        assert "user:alice" in rooms
        # Same seq reused across fanout? No - we want the client to
        # dedupe by request_id, not seq. Each emit is a fresh envelope.
        assert len(sio.emits) == 2

    async def test_approval_request_from_user_key_no_duplicate_fanout(self):
        """If the key is already user-level, don't double-emit."""
        sio = FakeSio()
        bus = SocketIOBus(sio=sio)
        await bus.publish(
            bus.user_key("alice"),
            {"type": "approval_request", "data": {"tool": "Bash"}},
        )
        assert len(sio.emits) == 1
        assert sio.emits[0][0] == "user:alice"

    async def test_emit_failure_is_swallowed(self):
        sio = FakeSio()
        sio.raise_on_emit = True
        bus = SocketIOBus(sio=sio)
        # Must not raise - the bus is best-effort transport
        result = await bus.publish(
            bus.session_key("a", "s", "u"),
            {"type": "token"},
        )
        assert result == 1

    async def test_publish_before_sio_wired_no_crash(self):
        """Lazy wiring: bus created with sio=None, emits must not NPE."""
        bus = SocketIOBus(sio=None)
        # The emit call will raise inside _emit but publish swallows it
        result = await bus.publish(
            bus.session_key("a", "s", "u"),
            {"type": "token"},
        )
        assert result == 1


# ══════════════════════════════════════════════════════════════════
# SocketIOBus - in-process handlers
# ══════════════════════════════════════════════════════════════════


class TestSocketIOBusHandlers:

    async def test_handler_receives_every_envelope(self):
        sio = FakeSio()
        bus = SocketIOBus(sio=sio)
        got: list[tuple[str, str]] = []

        async def h(user_id: str, env: dict) -> None:
            got.append((user_id, env["type"]))

        bus.add_handler(h)
        key = bus.session_key("a", "s", "alice")
        await bus.publish(key, {"type": "token"})
        await bus.publish(key, {"type": "result"})
        assert got == [("alice", "token"), ("alice", "result")]

    async def test_multiple_handlers_all_called(self):
        bus = SocketIOBus(sio=FakeSio())
        a_calls: list[str] = []
        b_calls: list[str] = []

        async def ha(uid, env): a_calls.append(env["type"])
        async def hb(uid, env): b_calls.append(env["type"])

        bus.add_handler(ha)
        bus.add_handler(hb)
        await bus.publish(bus.user_key("u"), {"type": "x"})
        assert a_calls == ["x"] and b_calls == ["x"]

    async def test_remove_handler_stops_calls(self):
        bus = SocketIOBus(sio=FakeSio())
        calls: list[str] = []

        async def h(uid, env): calls.append(env["type"])

        bus.add_handler(h)
        await bus.publish(bus.user_key("u"), {"type": "a"})
        bus.remove_handler(h)
        await bus.publish(bus.user_key("u"), {"type": "b"})
        assert calls == ["a"]

    async def test_handler_exception_isolated(self):
        """One bad handler mustn't break the others or the publish."""
        bus = SocketIOBus(sio=FakeSio())
        good: list[str] = []

        async def bad(uid, env): raise RuntimeError("boom")
        async def good_h(uid, env): good.append(env["type"])

        bus.add_handler(bad)
        bus.add_handler(good_h)
        result = await bus.publish(bus.user_key("u"), {"type": "x"})
        assert result == 1
        assert good == ["x"]

    async def test_remove_nonexistent_handler_noop(self):
        bus = SocketIOBus(sio=FakeSio())
        async def h(uid, env): pass
        # Must not raise
        bus.remove_handler(h)


# ══════════════════════════════════════════════════════════════════
# SocketIOBus - replay delegation
# ══════════════════════════════════════════════════════════════════


class TestSocketIOBusReplay:

    async def test_user_replay_filters_by_session(self):
        bus = SocketIOBus(sio=FakeSio())
        await bus.publish(bus.session_key("app1", "s1", "alice"), {"type": "a"})
        await bus.publish(bus.session_key("app1", "s2", "alice"), {"type": "b"})
        await bus.publish(bus.session_key("app1", "s1", "alice"), {"type": "c"})
        out = bus.user_replay("alice", 0, session_id="s1")
        assert [e["type"] for e in out] == ["a", "c"]

    async def test_user_latest_seq_tracks_publishes(self):
        bus = SocketIOBus(sio=FakeSio())
        assert bus.user_latest_seq("alice") == 0
        await bus.publish(bus.user_key("alice"), {"type": "x"})
        assert bus.user_latest_seq("alice") == 1
        await bus.publish(bus.user_key("alice"), {"type": "y"})
        assert bus.user_latest_seq("alice") == 2


# ══════════════════════════════════════════════════════════════════
# Event kind map (the contract with Flutter's router)
# ══════════════════════════════════════════════════════════════════


class TestEventKindMap:

    def test_session_events_tagged_session(self):
        for t in ("result", "turn_complete", "token", "tool_call",
                  "thinking", "hook", "memory_update"):
            assert _EVENT_KIND_MAP[t] == "session"

    def test_error_is_error_kind(self):
        assert _EVENT_KIND_MAP["error"] == "error"

    def test_approval_request_is_approval_kind(self):
        assert _EVENT_KIND_MAP["approval_request"] == "approval"

    def test_notification_is_background_activation_kind(self):
        assert _EVENT_KIND_MAP["notification"] == "background_activation"
        assert _EVENT_KIND_MAP["notification_result"] == "background_activation"

    async def test_unknown_type_defaults_to_session(self):
        bus = SocketIOBus(sio=FakeSio())
        await bus.publish(bus.user_key("u"), {"type": "brand_new_event"})
        assert bus.user_latest_seq("u") == 1
        out = bus.user_replay("u", 0)
        assert out[0]["kind"] == "session"


# ══════════════════════════════════════════════════════════════════
# InboxProducer - envelope → inbox row
# ══════════════════════════════════════════════════════════════════


class TestInboxProducer:

    async def _make(self) -> tuple[InboxProducer, SocketIOBus, FakeInboxStore]:
        bus = SocketIOBus(sio=FakeSio())
        store = FakeInboxStore()
        prod = InboxProducer(store=store, event_bus=bus, dispatcher=None)
        await prod.start()
        return prod, bus, store

    async def test_result_creates_session_completed_row(self):
        _, bus, store = await self._make()
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"),
            {"type": "result", "data": {"content": "done", "tokens": 42}},
        )
        assert len(store.items) == 1
        item = store.items[0]
        assert item["kind"] == InboxKind.SESSION_COMPLETED
        assert item["user_id"] == "alice"
        assert item["app_id"] == "myapp"
        assert item["session_id"] == "s1"
        assert "Myapp" in item["title"]  # title-cased app name
        assert item["subtitle"] == "done"
        assert item["metadata"]["tokens"] == 42

    async def test_result_with_error_skipped(self):
        """A ``result`` carrying an error is suppressed - the dedicated
        ``error`` event that follows will create the failed row."""
        _, bus, store = await self._make()
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"),
            {"type": "result", "data": {"error": "provider 500"}},
        )
        assert store.items == []

    async def test_result_with_aborted_error_still_creates_row(self):
        """``aborted`` is a user interrupt, not a failure - we keep it."""
        _, bus, store = await self._make()
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"),
            {"type": "result", "data": {"error": "aborted", "content": "partial"}},
        )
        assert len(store.items) == 1
        assert store.items[0]["kind"] == InboxKind.SESSION_COMPLETED

    async def test_turn_complete_alias(self):
        _, bus, store = await self._make()
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"),
            {"type": "turn_complete", "data": {"content": "ok"}},
        )
        assert len(store.items) == 1
        assert store.items[0]["kind"] == InboxKind.SESSION_COMPLETED

    async def test_error_creates_session_failed_row(self):
        _, bus, store = await self._make()
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"),
            {"type": "error", "data": {
                "error": "rate limit",
                "code": "rate_limited",
                "category": "rate_limit",
            }},
        )
        assert len(store.items) == 1
        item = store.items[0]
        assert item["kind"] == InboxKind.SESSION_FAILED
        assert item["subtitle"] == "rate limit"
        assert item["metadata"]["code"] == "rate_limited"

    async def test_credential_auth_required_creates_credential_row(self):
        _, bus, store = await self._make()
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"),
            {"type": "error", "data": {
                "code": "credential_auth_required",
                "provider": "openai",
            }},
        )
        assert len(store.items) == 1
        item = store.items[0]
        assert item["kind"] == InboxKind.CREDENTIAL_MISSING
        assert item["credential_provider"] == "openai"

    async def test_approval_request_creates_awaiting_row(self):
        _, bus, store = await self._make()
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"),
            {"type": "approval_request", "data": {"tool": "Bash"}},
        )
        # The bus fans out to session+user rooms, but handlers run per
        # publish call → one row (not two)
        assert len(store.items) == 1
        item = store.items[0]
        assert item["kind"] == InboxKind.SESSION_AWAITING_APPROVAL
        assert "Bash" in item["subtitle"]

    async def test_notification_result_creates_bg_completed_row(self):
        _, bus, store = await self._make()
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"),
            {"type": "notification_result", "data": {
                "content": "email sent", "activation_id": "act-1",
            }},
        )
        assert len(store.items) == 1
        item = store.items[0]
        assert item["kind"] == InboxKind.BG_ACTIVATION_COMPLETED
        assert item["activation_id"] == "act-1"

    async def test_ping_ignored(self):
        _, bus, store = await self._make()
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"), {"type": "ping"},
        )
        assert store.items == []

    async def test_unhandled_type_no_row(self):
        _, bus, store = await self._make()
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"), {"type": "token"},
        )
        # token events must NOT create inbox rows - they're live stream only
        assert store.items == []

    async def test_start_is_idempotent(self):
        _, bus, store = await self._make()
        bus_handlers_before = len(bus._handlers)
        # Second call must not register a second handler
        prod2 = InboxProducer(store=store, event_bus=bus, dispatcher=None)
        await prod2.start()
        # prod2 is a separate instance so it adds its handler - that's
        # fine. But the same instance calling start twice must not double.
        await prod2.start()
        assert len(bus._handlers) == bus_handlers_before + 1

    async def test_stop_removes_handler(self):
        prod, bus, store = await self._make()
        before = len(bus._handlers)
        await prod.stop()
        assert len(bus._handlers) == before - 1
        # Subsequent publish must NOT create rows
        await bus.publish(
            bus.session_key("myapp", "s1", "alice"),
            {"type": "result", "data": {"content": "x"}},
        )
        assert store.items == []

    async def test_store_failure_swallowed(self):
        _, bus, store = await self._make()
        store.raise_on_create = True
        # Must not propagate to publish
        result = await bus.publish(
            bus.session_key("a", "s", "u"),
            {"type": "result", "data": {"content": "x"}},
        )
        assert result == 1
        assert store.items == []


# ══════════════════════════════════════════════════════════════════
# ApprovalQueue - migration-critical fields & bus wiring
# ══════════════════════════════════════════════════════════════════


class TestApprovalRequestFields:

    def test_new_fields_present(self):
        req = ApprovalRequest(
            request_id="r1", agent_id="main", user_id="alice",
            tool_name="Bash", tool_params={}, risk_level="high",
            description="",
            app_id="myapp", session_id="sess-42",
        )
        assert req.app_id == "myapp"
        assert req.session_id == "sess-42"

    def test_to_dict_serializes_new_fields(self):
        req = ApprovalRequest(
            request_id="r1", agent_id="main", user_id="alice",
            tool_name="Bash", tool_params={}, risk_level="high",
            description="",
            app_id="myapp", session_id="sess-42",
        )
        d = req.to_dict()
        assert d["app_id"] == "myapp"
        assert d["session_id"] == "sess-42"
        assert d["request_id"] == "r1"
        assert d["tool_name"] == "Bash"
        # _future must not leak
        assert "_future" not in d

    def test_defaults_are_empty_string(self):
        req = ApprovalRequest(
            request_id="r", agent_id="a", user_id="u",
            tool_name="t", tool_params={}, risk_level="low", description="",
        )
        assert req.app_id == ""
        assert req.session_id == ""


class TestApprovalQueueWiring:

    async def test_enqueue_propagates_session_id_to_request(self):
        q = ApprovalQueue(default_timeout=1.0)
        seen: list[ApprovalRequest] = []

        async def h(req): seen.append(req)
        q.add_on_request(h)

        async def resolve_soon():
            await asyncio.sleep(0.02)
            for r in q.list_pending():
                q.resolve(r["request_id"], True)

        asyncio.create_task(resolve_soon())
        approved, _ = await q.enqueue(
            agent_id="main", tool_name="Bash", tool_params={},
            user_id="alice", session_id="sess-99", app_id="myapp",
        )
        assert approved is True
        assert len(seen) == 1
        assert seen[0].session_id == "sess-99"
        assert seen[0].app_id == "myapp"
        assert seen[0].user_id == "alice"

    async def test_enqueue_app_id_from_queue_attribute_fallback(self):
        """If the caller forgets ``app_id``, the queue's ``_app_id``
        attribute (set at deploy time by AppManager) is used."""
        q = ApprovalQueue(default_timeout=1.0)
        q._app_id = "myapp"
        seen: list[ApprovalRequest] = []

        async def h(req): seen.append(req)
        q.add_on_request(h)

        async def resolve_soon():
            await asyncio.sleep(0.02)
            for r in q.list_pending():
                q.resolve(r["request_id"], True)

        asyncio.create_task(resolve_soon())
        await q.enqueue(
            agent_id="main", tool_name="Bash", tool_params={},
            user_id="alice", session_id="sess-1",
        )
        assert seen[0].app_id == "myapp"

    async def test_bus_publish_callback_end_to_end(self):
        """The exact callback AppManager registers - verify it fans out
        correctly via the bus."""
        sio = FakeSio()
        bus = SocketIOBus(sio=sio)
        q = ApprovalQueue(default_timeout=1.0)
        q._app_id = "myapp"

        async def publisher(request):
            uid = request.user_id or "local"
            sid = request.session_id or ""
            if sid:
                key = bus.session_key("myapp", sid, uid)
            else:
                key = bus.user_key(uid)
            await bus.publish(key, {
                "type": "approval_request", "data": request.to_dict(),
            })
        q.add_on_request(publisher)

        async def resolve_soon():
            await asyncio.sleep(0.02)
            for r in q.list_pending():
                q.resolve(r["request_id"], True)

        asyncio.create_task(resolve_soon())
        approved, _ = await q.enqueue(
            agent_id="main", tool_name="Bash", tool_params={"cmd": "ls"},
            user_id="alice", session_id="sess-42",
            risk_level="high", description="run shell",
        )
        assert approved is True
        # Two emits: session + user fanout
        rooms = [r for r, _ in sio.emits]
        assert "session:sess-42" in rooms
        assert "user:alice" in rooms
        # Payload carries the dataclass fields
        env = sio.emits[0][1]
        p = env["payload"]
        assert p["app_id"] == "myapp"
        assert p["session_id"] == "sess-42"
        assert p["tool_name"] == "Bash"
        assert p["tool_params"] == {"cmd": "ls"}
        assert p["risk_level"] == "high"

    async def test_denied_approval_does_not_crash_publisher(self):
        sio = FakeSio()
        bus = SocketIOBus(sio=sio)
        q = ApprovalQueue(default_timeout=1.0)

        async def publisher(req):
            await bus.publish(
                bus.session_key("a", req.session_id, req.user_id),
                {"type": "approval_request", "data": req.to_dict()},
            )
        q.add_on_request(publisher)

        async def resolve_soon():
            await asyncio.sleep(0.02)
            for r in q.list_pending():
                q.resolve(r["request_id"], False, message="nope")

        asyncio.create_task(resolve_soon())
        approved, msg = await q.enqueue(
            agent_id="main", tool_name="t", tool_params={},
            user_id="u", session_id="s",
        )
        assert approved is False
        assert msg == "nope"
        assert len(sio.emits) >= 1

    async def test_timeout_still_publishes_request(self):
        sio = FakeSio()
        bus = SocketIOBus(sio=sio)
        q = ApprovalQueue(default_timeout=0.05)

        async def publisher(req):
            await bus.publish(
                bus.session_key("a", req.session_id, req.user_id),
                {"type": "approval_request", "data": req.to_dict()},
            )
        q.add_on_request(publisher)

        approved, msg = await q.enqueue(
            agent_id="main", tool_name="t", tool_params={},
            user_id="u", session_id="s",
        )
        assert approved is False
        assert "timed out" in msg.lower()
        # But the request WAS published so the client saw it (fans out to session + user rooms)
        assert len(sio.emits) == 2
        rooms = {e[0] for e in sio.emits}
        assert rooms == {"session:s", "user:u"}

    async def test_resolve_wrong_user_rejected(self):
        q = ApprovalQueue(default_timeout=0.2)
        async def h(req):
            pass
        q.add_on_request(h)

        async def try_resolve():
            await asyncio.sleep(0.02)
            for r in q.list_pending():
                # Wrong user - must be rejected
                ok = q.resolve(r["request_id"], True, user_id="mallory")
                assert ok is False
                # Correct user - accepted
                ok = q.resolve(r["request_id"], True, user_id="alice")
                assert ok is True

        asyncio.create_task(try_resolve())
        approved, _ = await q.enqueue(
            agent_id="main", tool_name="t", tool_params={},
            user_id="alice", session_id="s",
        )
        assert approved is True

    async def test_cancel_all_on_shutdown(self):
        q = ApprovalQueue(default_timeout=5.0)
        async def h(req):
            pass
        q.add_on_request(h)

        task = asyncio.create_task(q.enqueue(
            agent_id="main", tool_name="t", tool_params={},
            user_id="u", session_id="s",
        ))
        await asyncio.sleep(0.02)
        cancelled = q.cancel_all()
        assert cancelled == 1
        approved, _ = await task
        assert approved is False
