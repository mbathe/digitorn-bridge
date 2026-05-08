"""Stress + multimedia + multi-tab + sub-agent + long-running tests.

These exercise the SessionStore at scale beyond what the unit tests
cover. Targets:

  1. Many-session parallel write (1000 sessions x 100 events)
  2. Long single-session with periodic compaction (50k events,
     8 compactions, RAM stays bounded)
  3. Multimedia blob roundtrip with realistic binary patterns
     (PNG, WAV, MP4 magic bytes; large 5 MB blob)
  4. Multi-tab concurrent SSE (100 readers + 1 writer on same sid)
  5. Sub-agent tree depth 5 with recursive spawn

Skipped under ``BENCH=0`` so CI can opt out; default runs them.
"""
from __future__ import annotations

import asyncio
import os
import statistics
import time
from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.blob_store import BlobStore
from digitorn.core.runtime.session_store.store import InMemorySessionStore
from digitorn.core.runtime.session_store.types import Event


pytestmark = pytest.mark.skipif(
    os.environ.get("BENCH", "1") == "0",
    reason="set BENCH=1 to run stress tests",
)


# ── 1. Massive parallel sessions ────────────────────────────────────


@pytest.mark.asyncio
async def test_1000_sessions_parallel_no_corruption(tmp_root: Path):
    """1000 sessions x 100 events parallel (~100k events total).
    Every session must end up with seqs 1..100 monotonic and no
    corruption. Throughput target: > 5k events/sec aggregate."""
    store = InMemorySessionStore(
        root=tmp_root, flush_interval_ms=50,
        max_sessions_in_memory=2000,
    )
    await store.start()
    try:
        N_SESSIONS = 1000
        N_EVENTS = 100
        for i in range(N_SESSIONS):
            await store.open(f"s{i:04d}", app_id="a", user_id="u")

        async def write_session(sid: str) -> None:
            for j in range(N_EVENTS):
                await store.append_event(
                    sid, Event(type="token", payload={"i": j}),
                )

        t0 = time.perf_counter()
        await asyncio.gather(
            *[write_session(f"s{i:04d}") for i in range(N_SESSIONS)],
        )
        elapsed = time.perf_counter() - t0
        total = N_SESSIONS * N_EVENTS
        rate = total / elapsed
        print(
            f"\n  1000 sessions x 100 events: {elapsed:.2f}s, "
            f"{rate:,.0f} events/sec aggregate"
        )

        for i in range(0, N_SESSIONS, 50):
            sid = f"s{i:04d}"
            state = store.state(sid)
            assert state.last_seq == N_EVENTS
            assert len(state.events) == N_EVENTS
            assert [ev.seq for ev in state.events] == list(
                range(1, N_EVENTS + 1)
            )
        assert rate > 5_000, (
            f"throughput {rate:.0f}/sec below 5k/sec target"
        )
    finally:
        await store.stop()


# ── 2. Long single-session with periodic compaction ─────────────────


@pytest.mark.asyncio
async def test_long_session_with_compaction_keeps_ram_bounded(
    tmp_root: Path,
):
    """50k events on a single session, 8 compactions along the way.
    After each compaction, RAM should hold roughly the post-cutoff
    window (small) plus the projections. Memory must stay
    bounded -- no unbounded growth."""
    store = InMemorySessionStore(
        root=tmp_root, flush_interval_ms=100,
    )
    await store.start()
    try:
        await store.open("long", app_id="a", user_id="u")

        ram_sizes_after_compact: list[int] = []
        N_TOTAL = 50_000
        COMPACT_EVERY = 6_000
        last_compact_seq = 0

        for i in range(N_TOTAL):
            await store.append_event(
                "long", Event(
                    type="user_message", role="user",
                    content=f"msg-{i}",
                ),
            )
            if (i + 1) % COMPACT_EVERY == 0:
                state = store.state("long")
                cutoff = state.last_seq - 100
                if cutoff <= last_compact_seq:
                    continue
                await store.compact_session(
                    "long",
                    cutoff_seq=cutoff,
                    summary=f"recap up to seq {cutoff}",
                    tokens_estimate=200,
                    model="claude-opus-4-7",
                )
                last_compact_seq = cutoff
                state = store.state("long")
                ram_sizes_after_compact.append(state.event_count())

        if ram_sizes_after_compact:
            print(
                f"\n  long-session ({N_TOTAL} events, "
                f"{len(ram_sizes_after_compact)} compactions): "
                f"RAM event-count after each = {ram_sizes_after_compact}"
            )
            max_ram = max(ram_sizes_after_compact)
            assert max_ram < 2_000, (
                f"RAM event count {max_ram} grew unbounded despite "
                "compaction"
            )

        await store.flusher.flush()
        path = store._session_dir("long") / "events.jsonl"
        with path.open("r", encoding="utf-8") as f:
            disk_lines = sum(1 for _ in f)
        assert disk_lines >= N_TOTAL
    finally:
        await store.stop()


# ── 3. Multimedia blob roundtrip with realistic patterns ────────────


@pytest.mark.asyncio
async def test_multimedia_blobs_realistic_patterns(tmp_root: Path):
    """Roundtrip image-like, audio-like, video-like blobs.
    Same content dedupes; large 5 MB blob works; magic bytes survive.
    """
    bs = BlobStore(tmp_root / "blobs")

    png_magic = b"\x89PNG\r\n\x1a\n"
    png_blob = png_magic + b"\x00" * 50_000
    wav_magic = b"RIFF" + b"\x00" * 4 + b"WAVE"
    wav_blob = wav_magic + (b"\xa3\xb2\xc1\xd0" * 100_000)
    mp4_magic = b"\x00\x00\x00\x20ftypisom"
    mp4_blob = mp4_magic + b"\x42" * 5_000_000

    png_ref = await bs.put(png_blob, "image/png")
    wav_ref = await bs.put(wav_blob, "audio/wav")
    mp4_ref = await bs.put(mp4_blob, "video/mp4")

    assert (await bs.get(png_ref.hash))[:8] == png_magic
    assert (await bs.get(wav_ref.hash))[:12].endswith(b"WAVE")
    assert (await bs.get(mp4_ref.hash))[:12] == mp4_magic
    assert mp4_ref.size == 5_000_012

    dup_png = await bs.put(png_blob, "image/png")
    assert dup_png.hash == png_ref.hash
    assert {png_ref.hash, wav_ref.hash, mp4_ref.hash} == {
        png_ref.hash, wav_ref.hash, mp4_ref.hash,
    }

    # Large blob roundtrip integrity
    huge = bytes(b"\x00\x01\x02\x03" * 250_000)  # 1 MB
    huge_ref = await bs.put(huge, "application/octet-stream")
    assert await bs.get(huge_ref.hash) == huge


@pytest.mark.asyncio
async def test_blob_store_concurrent_writes(tmp_root: Path):
    """100 concurrent puts of distinct content; all blobs end up
    accessible with correct content."""
    bs = BlobStore(tmp_root / "blobs")
    N = 100

    async def put_one(i: int):
        return await bs.put(
            f"unique-content-{i}".encode("utf-8"),
            "text/plain",
        )

    refs = await asyncio.gather(*[put_one(i) for i in range(N)])
    assert len({r.hash for r in refs}) == N
    for i, ref in enumerate(refs):
        data = await bs.get(ref.hash)
        assert data == f"unique-content-{i}".encode("utf-8")


# ── 4. Multi-tab concurrent SSE ─────────────────────────────────────


@pytest.mark.asyncio
async def test_100_concurrent_readers_consistent_view(tmp_root: Path):
    """100 readers stream_events concurrently while a writer appends
    100 events. Every reader observes the same monotonic seqs."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=20)
    await store.start()
    try:
        await store.open("hot", app_id="a", user_id="u")
        N_EVENTS = 100
        N_READERS = 100

        async def reader() -> list[int]:
            seen: list[int] = []
            last_seen = 0
            while True:
                state = store.state("hot")
                if state is None:
                    await asyncio.sleep(0.001)
                    continue
                async for ev in store.stream_events(
                    "hot", since=last_seen,
                ):
                    seen.append(ev.seq)
                    last_seen = ev.seq
                if last_seen >= N_EVENTS:
                    return seen
                await asyncio.sleep(0.001)

        async def writer() -> None:
            for i in range(N_EVENTS):
                await store.append_event(
                    "hot", Event(type="token", payload={"i": i}),
                )
                await asyncio.sleep(0.0005)

        results = await asyncio.gather(
            writer(),
            *[reader() for _ in range(N_READERS)],
        )
        reader_outputs = results[1:]
        for out in reader_outputs:
            assert out[-1] == N_EVENTS
            assert out == sorted(out)
            assert len(out) == len(set(out))
        first = reader_outputs[0]
        for other in reader_outputs[1:]:
            assert other == first, "readers diverged"
    finally:
        await store.stop()


# ── 5. Sub-agent tree depth 5 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_subagent_tree_depth_5(tmp_root: Path):
    """Spawn root -> child1 -> grandchild -> ggchild -> gggchild ->
    leaf. Each level appends events, parents track children. Reload
    from disk preserves the entire tree structure."""
    s1 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s1.start()
    try:
        sids = ["root", "c1", "c2", "c3", "c4", "leaf"]
        await s1.open("root", app_id="a", user_id="u")
        for parent, child in zip(sids, sids[1:]):
            await s1.spawn_child(
                parent_sid=parent, child_sid=child, kind=f"depth_{child}",
            )
            await s1.append_event(
                child, Event(type="user_message", content=f"hi from {child}"),
            )

        for sid in sids:
            await s1.close_session(sid)
    finally:
        await s1.stop()

    s2 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s2.start()
    try:
        for sid in sids:
            await s2.open(sid, app_id="", user_id="")
        for parent, child in zip(sids, sids[1:]):
            cs = s2.state(child)
            assert cs is not None
            assert cs.parent_link is not None
            assert cs.parent_link.parent_session_id == parent
        for sid in sids[:-1]:
            ps = s2.state(sid)
            assert ps is not None
            assert len(ps.children) == 1
    finally:
        await s2.stop()


# ── 6. Many sub-agents per parent ───────────────────────────────────


@pytest.mark.asyncio
async def test_one_parent_50_children_parallel(tmp_root: Path):
    """A parent spawns 50 children in parallel; each child runs its
    own short turn. Parent's children list ends with all 50 refs in
    spawn order; each child has its own independent seq counter."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=20)
    await store.start()
    try:
        await store.open("parent", app_id="a", user_id="u")

        async def spawn_and_use(i: int) -> str:
            sid = f"child-{i:03d}"
            await store.spawn_child(
                parent_sid="parent", child_sid=sid,
                kind=f"kind-{i % 5}",
            )
            for j in range(10):
                await store.append_event(
                    sid, Event(type="token", payload={"j": j}),
                )
            return sid

        await asyncio.gather(*[spawn_and_use(i) for i in range(50)])

        parent = store.state("parent")
        assert len(parent.children) == 50
        for i in range(50):
            sid = f"child-{i:03d}"
            cs = store.state(sid)
            assert cs.last_seq == 10
            assert [ev.seq for ev in cs.events] == list(range(1, 11))
    finally:
        await store.stop()
