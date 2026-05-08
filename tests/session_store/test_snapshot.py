"""Snapshot: build, write, read, fast reopen contract."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.snapshot import (
    build_snapshot, read_snapshot, write_snapshot,
)
from digitorn.core.runtime.session_store.store import InMemorySessionStore
from digitorn.core.runtime.session_store.types import Event


@pytest.mark.asyncio
async def test_close_writes_snapshot(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        await store.append_event("s", Event(
            type="user_message", role="user", content="hi",
        ))
        await store.append_event("s", Event(
            type="assistant_message", role="assistant", content="hello",
            payload={"prompt_tokens": 3, "completion_tokens": 1, "cost": 0.0001},
        ))
        await store.append_event("s", Event(
            type="todo_add", payload={"id": "t1", "text": "ship"},
        ))
        await store.close_session("s")
        snap_path = store._session_dir("s") / "snapshot.json"
        assert snap_path.exists()
        data = json.loads(snap_path.read_text())
        assert data["session_id"] == "s"
        assert data["app_id"] == "a"
        assert data["closed"] is True
        assert data["last_seq"] == 3
        assert data["event_count"] == 3
        assert len(data["messages"]) == 2
        assert data["messages"][0]["content"] == "hi"
        assert data["messages"][1]["content"] == "hello"
        assert len(data["todos"]) == 1
        assert data["todos"][0]["text"] == "ship"
        assert data["tokens_in"] == 3
        assert data["tokens_out"] == 1
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_read_snapshot_after_restart(tmp_root: Path):
    s1 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s1.start()
    try:
        await s1.open("s", app_id="a", user_id="u")
        for i in range(20):
            await s1.append_event("s", Event(
                type="user_message", content=f"msg-{i}",
            ))
        await s1.close_session("s")
    finally:
        await s1.stop()

    s2 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s2.start()
    try:
        snap = await s2.read_snapshot("s")
        assert snap is not None
        assert snap["session_id"] == "s"
        assert snap["last_seq"] == 20
        assert len(snap["messages"]) == 20
        assert snap["messages"][-1]["content"] == "msg-19"
    finally:
        await s2.stop()


@pytest.mark.asyncio
async def test_read_snapshot_missing_returns_none(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root)
    await store.start()
    try:
        assert await store.read_snapshot("nope") is None
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_save_snapshot_without_close(tmp_root: Path):
    """Long-running session: periodic checkpoint without ending."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        await store.append_event("s", Event(type="user_message", content="hi"))
        ok = await store.save_snapshot("s")
        assert ok is True
        snap = await store.read_snapshot("s")
        assert snap is not None
        assert snap["last_seq"] == 1
        assert snap["closed"] is False
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_save_snapshot_unknown_returns_false(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root)
    await store.start()
    try:
        ok = await store.save_snapshot("nope")
        assert ok is False
    finally:
        await store.stop()


def test_build_snapshot_pure_function(tmp_root: Path):
    """build_snapshot is a pure function (no I/O), used standalone
    for tests + tooling."""
    from digitorn.core.runtime.session_store.session_state import SessionState
    state = SessionState(session_id="s", app_id="a", user_id="u")
    state.last_seq = 5
    state.first_seq = 1
    state.tokens_in = 100
    state.tokens_out = 50
    snap = build_snapshot(state)
    assert snap["session_id"] == "s"
    assert snap["last_seq"] == 5
    assert snap["tokens_in"] == 100
    assert snap["tokens_out"] == 50
    assert snap["messages"] == []
    assert snap["children"] == []


def test_write_then_read_roundtrip(tmp_root: Path):
    sd = tmp_root / "s1"
    snap = {"session_id": "s1", "messages": [{"role": "user", "content": "x"}]}
    write_snapshot(sd, snap)
    assert read_snapshot(sd) == snap


def test_read_corrupt_returns_none(tmp_root: Path):
    sd = tmp_root / "s1"
    sd.mkdir()
    (sd / "snapshot.json").write_text("not json {{{", encoding="utf-8")
    assert read_snapshot(sd) is None


def test_read_missing_returns_none(tmp_root: Path):
    assert read_snapshot(tmp_root / "doesnotexist") is None
