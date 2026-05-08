"""Reader: high-level helpers for messages, snapshot, replay."""
from __future__ import annotations

from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.reader import (
    latest_seq, load_messages_for_llm, load_snapshot,
    replay_events_since, session_summary,
)
from digitorn.core.runtime.session_store.store import InMemorySessionStore
from digitorn.core.runtime.session_store.types import Event


@pytest.mark.asyncio
async def test_load_messages_for_llm(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        await store.append_event("s", Event(
            type="user_message", role="user", content="ping",
        ))
        await store.append_event("s", Event(
            type="assistant_message", role="assistant", content="pong",
            tool_calls=[{"id": "t1", "name": "f"}],
        ))
        msgs = load_messages_for_llm(store, "s")
        assert len(msgs) == 2
        assert msgs[0] == {"role": "user", "content": "ping"}
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "pong"
        assert msgs[1]["tool_calls"] == [{"id": "t1", "name": "f"}]
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_load_messages_unknown_returns_empty(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root)
    await store.start()
    try:
        assert load_messages_for_llm(store, "nope") == []
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_load_snapshot_returns_dict(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        await store.append_event("s", Event(
            type="user_message", role="user", content="hi",
        ))
        await store.close_session("s")
        snap = await load_snapshot(store, "s")
        assert snap is not None
        assert snap["session_id"] == "s"
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_replay_events_since(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        for i in range(10):
            await store.append_event("s", Event(type="x", payload={"i": i}))

        seqs = []
        async for ev in replay_events_since(store, "s", since=5):
            seqs.append(ev.seq)
        assert seqs == [6, 7, 8, 9, 10]
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_latest_seq(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        assert latest_seq(store, "s") == 0
        await store.open("s", app_id="a", user_id="u")
        for _ in range(7):
            await store.append_event("s", Event(type="x"))
        assert latest_seq(store, "s") == 7
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_session_summary(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        assert session_summary(store, "s") is None
        await store.open("s", app_id="myapp", user_id="me")
        await store.append_event("s", Event(type="user_message", content="x"))
        s = session_summary(store, "s")
        assert s is not None
        assert s["session_id"] == "s"
        assert s["app_id"] == "myapp"
        assert s["user_id"] == "me"
        assert s["last_seq"] == 1
    finally:
        await store.stop()
