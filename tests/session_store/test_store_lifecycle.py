"""InMemorySessionStore lifecycle: open, append, close, reopen, replay."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.store import InMemorySessionStore
from digitorn.core.runtime.session_store.types import Event


@pytest.mark.asyncio
async def test_open_creates_session(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root)
    await store.start()
    try:
        state = await store.open("sid1", app_id="app", user_id="u")
        assert state.session_id == "sid1"
        assert state.app_id == "app"
        assert state.user_id == "u"
        assert state.pinned is True
        assert state.last_seq == 0
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_append_assigns_monotonic_seq(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        s1 = await store.append_event("s", Event(type="user_message", content="a"))
        s2 = await store.append_event("s", Event(type="assistant_message", content="b"))
        s3 = await store.append_event("s", Event(type="token", content="c"))
        assert (s1, s2, s3) == (1, 2, 3)
        state = store.state("s")
        assert state.last_seq == 3
        assert state.event_count() == 3
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_append_runs_projection(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        await store.append_event("s", Event(type="user_message", content="hi"))
        await store.append_event("s", Event(
            type="assistant_message", content="hello",
            payload={"prompt_tokens": 5, "completion_tokens": 3, "cost": 0.001},
        ))
        await store.append_event("s", Event(type="token", payload={"text": "h"}))
        state = store.state("s")
        assert len(state.messages) == 2
        assert state.messages[0].role == "user"
        assert state.messages[0].content == "hi"
        assert state.messages[1].role == "assistant"
        assert state.messages[1].content == "hello"
        assert state.tokens_in == 5
        assert state.tokens_out == 3
        assert state.cost_total == 0.001
        assert state.event_count() == 3
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_append_persists_to_disk(tmp_root: Path):
    store = InMemorySessionStore(
        root=tmp_root, flush_interval_ms=10,
    )
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        for i in range(20):
            await store.append_event("s", Event(type="x", payload={"i": i}))
        await store.flusher.flush()
        path = store._session_dir("s") / "events.jsonl"
        assert path.exists()
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 20
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_append_with_no_open_raises(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root)
    await store.start()
    try:
        with pytest.raises(KeyError, match="session_not_open"):
            await store.append_event("nope", Event(type="x"))
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_close_marks_meta_closed(tmp_root: Path):
    store = InMemorySessionStore(
        root=tmp_root, flush_interval_ms=10,
    )
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        await store.append_event("s", Event(type="x"))
        await store.close_session("s")
        from digitorn.core.runtime.session_store.meta_io import MetaIO
        meta = MetaIO.read(store._session_dir("s"))
        assert meta is not None
        assert meta["closed"] is True
        assert meta["ended_at"] is not None
        assert meta["event_count"] == 1
        state = store.state("s")
        assert state.pinned is False
        assert state.closed is True
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_state_returns_none_for_unknown(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root)
    await store.start()
    try:
        assert store.state("never-opened") is None
    finally:
        await store.stop()
