"""Sub-agent tree: spawn, parent <-> child cross-link, recursive
spawning, replay, byte-identical reconstruction."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.store import InMemorySessionStore
from digitorn.core.runtime.session_store.types import Event


@pytest.mark.asyncio
async def test_spawn_child_emits_agent_spawn_on_parent(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("parent", app_id="a", user_id="u")
        await store.append_event("parent", Event(type="user_message", content="go"))

        child = await store.spawn_child(
            parent_sid="parent", child_sid="child-1", kind="explore",
        )

        parent = store.state("parent")
        assert any(ev.type == "agent_spawn" for ev in parent.events)
        spawn_ev = [e for e in parent.events if e.type == "agent_spawn"][0]
        assert spawn_ev.payload["run_id"] == "child-1"
        assert spawn_ev.payload["kind"] == "explore"

        assert len(parent.children) == 1
        assert parent.children[0].run_id == "child-1"
        assert parent.children[0].kind == "explore"

        assert child.parent_link is not None
        assert child.parent_link.parent_session_id == "parent"
        assert child.parent_link.child_kind == "explore"
        assert child.session_id == "child-1"
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_child_inherits_parent_app_user_when_omitted(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("p", app_id="myapp", user_id="paul")
        child = await store.spawn_child(
            parent_sid="p", child_sid="c", kind="worker",
        )
        assert child.app_id == "myapp"
        assert child.user_id == "paul"
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_child_can_have_own_seq_independent_of_parent(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("p", app_id="a", user_id="u")
        await store.append_event("p", Event(type="user_message", content="x"))
        await store.spawn_child(parent_sid="p", child_sid="c", kind="w")

        for i in range(5):
            seq = await store.append_event("c", Event(
                type="user_message", content=f"child-{i}",
            ))
            assert seq == i + 1

        assert store.state("p").last_seq == 2
        assert store.state("c").last_seq == 5
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_parent_link_persisted_on_disk(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("p", app_id="a", user_id="u")
        await store.spawn_child(parent_sid="p", child_sid="c", kind="kk")
        await store.flusher.flush()
        path = store._session_dir("c") / "parent_link.json"
        assert path.exists()
        link = json.loads(path.read_text())
        assert link["parent_session_id"] == "p"
        assert link["child_kind"] == "kk"
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_child_reload_from_disk_preserves_parent_link(tmp_root: Path):
    s1 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s1.start()
    try:
        await s1.open("p", app_id="a", user_id="u")
        await s1.spawn_child(parent_sid="p", child_sid="c", kind="explore")
        await s1.append_event("c", Event(type="user_message", content="hello"))
        await s1.close_session("c")
        await s1.close_session("p")
    finally:
        await s1.stop()

    s2 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s2.start()
    try:
        child = await s2.open("c", app_id="", user_id="")
        assert child.parent_link is not None
        assert child.parent_link.parent_session_id == "p"
        assert child.parent_link.child_kind == "explore"
        assert child.last_seq == 1
        assert len(child.messages) == 1
    finally:
        await s2.stop()


@pytest.mark.asyncio
async def test_grandchild_recursive_spawn(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("p", app_id="a", user_id="u")
        await store.spawn_child(parent_sid="p", child_sid="c", kind="parent_agent")
        await store.spawn_child(parent_sid="c", child_sid="g", kind="leaf_agent")

        c = store.state("c")
        g = store.state("g")
        assert g.parent_link.parent_session_id == "c"
        assert len(c.children) == 1
        assert c.children[0].run_id == "g"
        assert g.children == []
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_list_children_returns_in_memory_refs(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("p", app_id="a", user_id="u")
        for i in range(3):
            await store.spawn_child(
                parent_sid="p", child_sid=f"c{i}", kind=f"kind-{i}",
            )
        children = store.list_children("p")
        assert len(children) == 3
        assert {c.run_id for c in children} == {"c0", "c1", "c2"}
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_list_children_unknown_parent_returns_empty(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root)
    await store.start()
    try:
        assert store.list_children("nope") == []
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_spawn_child_unknown_parent_raises(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root)
    await store.start()
    try:
        with pytest.raises(KeyError, match="parent_session_not_open"):
            await store.spawn_child(
                parent_sid="nope", child_sid="c", kind="x",
            )
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_agent_result_marks_child_completed(tmp_root: Path):
    """Emit agent_result on the parent. The projection updates the
    matching ChildAgentRef status + completed_at."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("p", app_id="a", user_id="u")
        await store.spawn_child(parent_sid="p", child_sid="c", kind="w")

        await store.append_event("p", Event(
            type="agent_result",
            success=True,
            payload={"run_id": "c", "summary": "all good"},
        ))

        children = store.list_children("p")
        assert len(children) == 1
        ch = children[0]
        assert ch.status == "completed"
        assert ch.result_summary == "all good"
        assert ch.completed_at is not None
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_snapshot_includes_children(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("p", app_id="a", user_id="u")
        await store.spawn_child(parent_sid="p", child_sid="c1", kind="x")
        await store.spawn_child(parent_sid="p", child_sid="c2", kind="y")
        await store.close_session("p")
        snap = await store.read_snapshot("p")
        assert snap is not None
        assert len(snap["children"]) == 2
        kinds = {c["kind"] for c in snap["children"]}
        assert kinds == {"x", "y"}
    finally:
        await store.stop()
