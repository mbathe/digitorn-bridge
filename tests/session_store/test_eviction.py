"""LRU eviction + pinning."""
from __future__ import annotations

from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.store import InMemorySessionStore
from digitorn.core.runtime.session_store.types import Event


@pytest.mark.asyncio
async def test_pinned_session_never_evicted(tmp_root: Path):
    store = InMemorySessionStore(
        root=tmp_root,
        flush_interval_ms=10,
        max_sessions_in_memory=2,
    )
    await store.start()
    try:
        for i in range(5):
            sid = f"s{i}"
            await store.open(sid, app_id="a", user_id="u", pin=True)
            await store.append_event(sid, Event(type="x"))
        in_mem = store.list_in_memory_sessions()
        assert len(in_mem) == 5
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_unpinned_session_evicted_under_pressure(tmp_root: Path):
    store = InMemorySessionStore(
        root=tmp_root,
        flush_interval_ms=10,
        max_sessions_in_memory=2,
    )
    await store.start()
    try:
        for i in range(5):
            sid = f"s{i}"
            await store.open(sid, app_id="a", user_id="u", pin=False)
            await store.append_event(sid, Event(type="x"))
        import asyncio
        await asyncio.sleep(0.1)
        await store._maybe_evict()
        in_mem = store.list_in_memory_sessions()
        assert len(in_mem) <= 2
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_evict_unpinned_succeeds(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u", pin=False)
        await store.append_event("s", Event(type="x"))
        ok = await store.evict("s")
        assert ok is True
        assert store.state("s") is None
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_evict_pinned_refuses(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u", pin=True)
        await store.append_event("s", Event(type="x"))
        ok = await store.evict("s")
        assert ok is False
        assert store.state("s") is not None
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_evict_unknown_returns_false(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root)
    await store.start()
    try:
        ok = await store.evict("nope")
        assert ok is False
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_evicted_session_reloads_from_disk(tmp_root: Path):
    """After eviction, opening again reloads the full state from
    events.jsonl. This is the cold-load path."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        state = await store.open("s", app_id="a", user_id="u", pin=False)
        for i in range(20):
            await store.append_event(
                "s", Event(type="user_message", content=f"m{i}"),
            )
        state.pinned = False
        ok = await store.evict("s")
        assert ok is True

        reloaded = await store.open("s", app_id="a", user_id="u")
        assert reloaded.last_seq == 20
        assert reloaded.event_count() == 20
        assert len(reloaded.messages) == 20
        assert reloaded.messages[-1].content == "m19"
    finally:
        await store.stop()
