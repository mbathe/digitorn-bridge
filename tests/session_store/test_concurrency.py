"""Concurrency: multi-tab read while writing, threadpool writers."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.store import InMemorySessionStore
from digitorn.core.runtime.session_store.types import Event


@pytest.mark.asyncio
async def test_multi_tab_simultaneous_readers(tmp_root: Path):
    """3 SSE-style consumers reading the same session as the agent
    appends. All 3 see the same events with the same seqs."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")

        async def consumer(stop_at: int) -> list[int]:
            out = []
            while True:
                state = store.state("s")
                if state is None:
                    await asyncio.sleep(0.001)
                    continue
                async for ev in store.stream_events("s", since=out[-1] if out else 0):
                    out.append(ev.seq)
                    if ev.seq >= stop_at:
                        return out
                await asyncio.sleep(0.001)

        N = 50
        async def writer():
            for i in range(N):
                await store.append_event(
                    "s", Event(type="token", payload={"i": i}),
                )
                await asyncio.sleep(0.001)

        results = await asyncio.gather(
            writer(), consumer(N), consumer(N), consumer(N),
        )
        c1, c2, c3 = results[1], results[2], results[3]
        assert c1[-1] == N
        assert c2[-1] == N
        assert c3[-1] == N
        assert c1 == c2 == c3
        assert c1 == sorted(c1)
        assert len(c1) == len(set(c1))
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_threadpool_writer_seq_consistent(tmp_root: Path):
    """SeqAllocator is threading.Lock-based, so threadpool callers
    coexist correctly with asyncio callers. Verify by writing from
    a thread pool while the main asyncio loop is running."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=20)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        results: list[int] = []
        results_lock = threading.Lock()

        def thread_worker(n: int):
            local = []
            for _ in range(n):
                local.append(store.allocator.next(session_id="s"))
            with results_lock:
                results.extend(local)

        threads = [
            threading.Thread(target=thread_worker, args=(50,))
            for _ in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 1000
        assert len(set(results)) == 1000
        assert sorted(results) == list(range(1, 1001))
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_separate_sessions_no_interference(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        for sid in ["a", "b", "c"]:
            await store.open(sid, app_id="x", user_id="u")

        async def write_for(sid: str, n: int):
            for i in range(n):
                await store.append_event(
                    sid, Event(type="user_message", content=f"{sid}-{i}"),
                )

        await asyncio.gather(
            write_for("a", 30),
            write_for("b", 20),
            write_for("c", 50),
        )

        assert store.state("a").last_seq == 30
        assert store.state("b").last_seq == 20
        assert store.state("c").last_seq == 50
        assert store.state("a").messages[-1].content == "a-29"
        assert store.state("b").messages[-1].content == "b-19"
        assert store.state("c").messages[-1].content == "c-49"
    finally:
        await store.stop()
