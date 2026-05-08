"""Crash + restart recovery: cold load preserves state byte-identical."""
from __future__ import annotations

from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.store import InMemorySessionStore
from digitorn.core.runtime.session_store.types import Event


@pytest.mark.asyncio
async def test_close_then_reopen_replays_state(tmp_root: Path):
    s1 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s1.start()
    try:
        await s1.open("sid", app_id="myapp", user_id="me")
        await s1.append_event("sid", Event(
            type="user_message", role="user", content="bonjour",
        ))
        await s1.append_event("sid", Event(
            type="assistant_message", role="assistant", content="salut",
            payload={"prompt_tokens": 4, "completion_tokens": 2, "cost": 0.0005},
        ))
        await s1.append_event("sid", Event(
            type="todo_add", payload={"id": "t1", "text": "ship feature"},
        ))
        await s1.close_session("sid")
    finally:
        await s1.stop()

    s2 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s2.start()
    try:
        state = await s2.open("sid", app_id="", user_id="")
        assert state.app_id == "myapp"
        assert state.user_id == "me"
        assert state.last_seq == 3
        assert len(state.messages) == 2
        assert state.messages[0].content == "bonjour"
        assert state.messages[1].content == "salut"
        assert state.tokens_in == 4
        assert state.tokens_out == 2
        assert state.cost_total == 0.0005
        assert len(state.todos) == 1
        assert state.todos[0].text == "ship feature"
        assert state.closed is True
    finally:
        await s2.stop()


@pytest.mark.asyncio
async def test_seq_continues_after_reopen(tmp_root: Path):
    """The killer test: seqs do NOT recycle across daemon restart."""
    s1 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s1.start()
    try:
        await s1.open("sid", app_id="a", user_id="u")
        for i in range(5):
            await s1.append_event("sid", Event(type="x", payload={"i": i}))
        await s1.close_session("sid")
    finally:
        await s1.stop()

    s2 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s2.start()
    try:
        await s2.open("sid", app_id="a", user_id="u")
        seq = await s2.append_event("sid", Event(type="x", payload={"i": 99}))
        assert seq == 6
        seq2 = await s2.append_event("sid", Event(type="x"))
        assert seq2 == 7
    finally:
        await s2.stop()


@pytest.mark.asyncio
async def test_meta_corrupt_falls_back_to_jsonl_tail(tmp_root: Path):
    s1 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s1.start()
    try:
        await s1.open("sid", app_id="a", user_id="u")
        for _ in range(10):
            await s1.append_event("sid", Event(type="x"))
        await s1.flusher.flush()
        meta_path = s1._session_dir("sid") / "meta.json"
    finally:
        await s1.stop()

    meta_path.write_text("garbage", encoding="utf-8")

    s2 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s2.start()
    try:
        await s2.open("sid", app_id="a", user_id="u")
        seq = await s2.append_event("sid", Event(type="x"))
        assert seq == 11
    finally:
        await s2.stop()


@pytest.mark.asyncio
async def test_unknown_session_returns_no_events(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root)
    await store.start()
    try:
        events = []
        async for ev in store.stream_events("nope"):
            events.append(ev)
        assert events == []
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_stream_events_since(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        for i in range(5):
            await store.append_event("s", Event(type="x", payload={"i": i}))

        out = []
        async for ev in store.stream_events("s", since=2):
            out.append(ev.seq)
        assert out == [3, 4, 5]
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_full_persistence_no_filtering(tmp_root: Path):
    """Every event type goes to disk. No filtering. The streaming
    chunks (token, thinking_delta, heartbeat) MUST end up persisted
    exactly like in the legacy Postgres path."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        types = [
            "user_message", "token", "thinking_delta", "thinking_started",
            "out_token", "in_token", "tool_call_streaming", "stream_done",
            "turn:heartbeat", "turn:start", "turn:end", "hook",
            "behavior:warning", "preview:delta", "preview:state",
            "agent_progress", "tool_call", "tool_result",
            "assistant_message", "memory_remember",
        ]
        for t in types:
            await store.append_event("s", Event(type=t))
        await store.flusher.flush()
        path = store._session_dir("s") / "events.jsonl"
        lines = path.read_text().strip().splitlines()
        assert len(lines) == len(types)
        import json as _json
        on_disk_types = [_json.loads(ln)["type"] for ln in lines]
        assert on_disk_types == types
    finally:
        await store.stop()
