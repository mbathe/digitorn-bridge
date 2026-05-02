"""Tests for the universal preview module (core/modules/preview).

These exercise:

- Basic state mutations (set_state / patch_state / clear)
- Canvas mutations (push_node, update_node, highlight_node, remove_node,
  push_edge, remove_edge) - including cascade-drop of touching edges
- Per-session isolation: two different session ids must not see each
  other's state
- Socket.IO bus publishing: events are emitted via the injected event bus
- Snapshot replay: snapshot_for returns the full current state with an
  incrementing seq

Everything runs without the full daemon - we instantiate PreviewModule
directly.
"""

from __future__ import annotations

import asyncio

import pytest

from digitorn.modules.preview.module import (
    ClearParams,
    EmitParams,
    HighlightNodeParams,
    PatchStateParams,
    PreviewModule,
    PushEdgeParams,
    PushNodeParams,
    RemoveEdgeParams,
    RemoveNodeParams,
    SetStateParams,
    UpdateNodeParams,
)


def _fresh() -> PreviewModule:
    m = PreviewModule()
    m.set_active_session("sess-A")
    return m


def test_set_state_publishes_to_snapshot():
    m = _fresh()

    async def go():
        await m.set_state(SetStateParams(key="phase", value="discovery"))
        snap = m.snapshot_for("sess-A")
        assert snap["state"] == {"phase": "discovery"}
        assert snap["seq"] == 1
        assert len(snap["events"]) == 1

    asyncio.run(go())


def test_patch_state_merges():
    m = _fresh()

    async def go():
        await m.set_state(SetStateParams(key="a", value=1))
        await m.patch_state(PatchStateParams(patch={"b": 2, "c": 3}))
        snap = m.snapshot_for("sess-A")
        assert snap["state"] == {"a": 1, "b": 2, "c": 3}

    asyncio.run(go())


def test_push_node_then_highlight_then_remove():
    m = _fresh()

    async def go():
        await m.push_node(
            PushNodeParams(
                id="n1", type="state", label="Interview",
                position={"x": 0, "y": 0},
            )
        )
        await m.highlight_node(HighlightNodeParams(id="n1", status="running"))
        snap = m.snapshot_for("sess-A")
        assert len(snap["nodes"]) == 1
        assert snap["nodes"][0]["status"] == "running"

        await m.remove_node(RemoveNodeParams(id="n1"))
        snap = m.snapshot_for("sess-A")
        assert snap["nodes"] == []

    asyncio.run(go())


def test_update_node_partial_merges_into_existing():
    m = _fresh()

    async def go():
        await m.push_node(
            PushNodeParams(id="n1", label="A", position={"x": 0, "y": 0})
        )
        await m.update_node(
            UpdateNodeParams(id="n1", updates={"label": "A renamed", "foo": "bar"})
        )
        snap = m.snapshot_for("sess-A")
        node = snap["nodes"][0]
        assert node["label"] == "A renamed"
        # Unknown keys land in node.data
        assert node["data"]["foo"] == "bar"

    asyncio.run(go())


def test_update_node_missing_returns_error():
    m = _fresh()

    async def go():
        result = await m.update_node(
            UpdateNodeParams(id="nope", updates={"label": "X"})
        )
        assert result.success is False
        assert "not found" in result.error.lower()

    asyncio.run(go())


def test_push_edge_and_cascade_drop_on_node_remove():
    m = _fresh()

    async def go():
        await m.push_node(PushNodeParams(id="n1", position={"x": 0, "y": 0}))
        await m.push_node(PushNodeParams(id="n2", position={"x": 1, "y": 0}))
        await m.push_edge(PushEdgeParams(id="e1", source="n1", target="n2"))
        snap = m.snapshot_for("sess-A")
        assert len(snap["edges"]) == 1

        # Removing n1 should cascade-drop e1
        await m.remove_node(RemoveNodeParams(id="n1"))
        snap = m.snapshot_for("sess-A")
        assert snap["edges"] == []

    asyncio.run(go())


def test_clear_wipes_everything():
    m = _fresh()

    async def go():
        await m.set_state(SetStateParams(key="a", value=1))
        await m.push_node(PushNodeParams(id="n1", position={"x": 0, "y": 0}))
        await m.emit(EmitParams(event_type="x", data={"foo": 1}))

        await m.clear(ClearParams())
        snap = m.snapshot_for("sess-A")
        assert snap["state"] == {}
        assert snap["nodes"] == []
        assert snap["edges"] == []
        assert snap["resources"] == {}

    asyncio.run(go())


def test_per_session_isolation():
    m = PreviewModule()

    async def go():
        m.set_active_session("alice")
        await m.set_state(SetStateParams(key="shared", value="alice-value"))

        m.set_active_session("bob")
        await m.set_state(SetStateParams(key="shared", value="bob-value"))

        snap_a = m.snapshot_for("alice")
        snap_b = m.snapshot_for("bob")
        assert snap_a["state"] == {"shared": "alice-value"}
        assert snap_b["state"] == {"shared": "bob-value"}
        # Each session has its own seq counter
        assert snap_a["seq"] == 1
        assert snap_b["seq"] == 1

    asyncio.run(go())


class _MockBus:
    """Minimal mock for SocketIOBus to capture published events."""

    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    @staticmethod
    def session_key(app_id: str, session_id: str, user_id: str) -> str:
        return f"{app_id}:{session_id}:{user_id}"

    async def publish(self, key: str, data: dict) -> None:
        self.published.append((key, data))


def _fresh_with_bus() -> tuple[PreviewModule, _MockBus]:
    m = PreviewModule()
    m.set_active_session("sess-A", user_id="u1")
    bus = _MockBus()
    m._event_bus = bus
    m._bus_app_id = "test-app"
    return m, bus


def test_bus_receives_events():
    m, bus = _fresh_with_bus()

    async def go():
        await m.set_state(SetStateParams(key="k", value="v"))
        await asyncio.sleep(0.05)  # let create_task fire
        assert len(bus.published) == 1
        key, data = bus.published[0]
        assert "sess-A" in key
        assert data["type"] == "preview:state_changed"
        assert data["data"]["key"] == "k"

    asyncio.run(go())


def test_cleanup_session_drops_state():
    m = _fresh()

    async def go():
        await m.set_state(SetStateParams(key="k", value="v"))
        await m.cleanup_session("sess-A")
        snap = m.snapshot_for("sess-A")
        assert snap["state"] == {}

    asyncio.run(go())


def test_emit_publishes_arbitrary_event_type():
    m, bus = _fresh_with_bus()

    async def go():
        await m.emit(
            EmitParams(event_type="compile_attempt", data={"attempt": 1})
        )
        await asyncio.sleep(0.05)
        assert len(bus.published) == 1
        _, data = bus.published[0]
        assert data["type"] == "preview:compile_attempt"
        assert data["data"]["attempt"] == 1

    asyncio.run(go())


def test_remove_edge():
    m = _fresh()

    async def go():
        await m.push_node(PushNodeParams(id="n1", position={"x": 0, "y": 0}))
        await m.push_node(PushNodeParams(id="n2", position={"x": 1, "y": 0}))
        await m.push_edge(PushEdgeParams(id="e1", source="n1", target="n2"))
        await m.remove_edge(RemoveEdgeParams(id="e1"))
        snap = m.snapshot_for("sess-A")
        assert snap["edges"] == []
        # nodes survive
        assert len(snap["nodes"]) == 2

    asyncio.run(go())
