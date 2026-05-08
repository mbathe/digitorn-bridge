"""Crash recovery audit: simulate process death at every meaningful
moment and verify the next instance recovers without data loss
(beyond the 50ms flush window).

Failures we cover:
  * kill -9 mid-write of events.jsonl  -> latest line may be partial,
    next reload skips it; seq counter resumes from last GOOD line
  * kill -9 between events.jsonl flush and meta.json write
    -> meta is stale; reload uses jsonl_tail to seed seq monotonic
  * kill -9 after compact_session writes compaction.json but before
    snapshot.json  -> reload sees compaction without snapshot;
    falls back to replaying events from cutoff
  * snapshot.json corrupt mid-write  -> reload ignores it and
    rebuilds projection from events
  * meta.json corrupt mid-write  -> seq seeded from jsonl tail
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.compaction import (
    Compaction, write_compaction,
)
from digitorn.core.runtime.session_store.store import InMemorySessionStore
from digitorn.core.runtime.session_store.types import Event


@pytest.mark.asyncio
async def test_partial_jsonl_line_skipped_on_reload(tmp_root: Path):
    """Append a torn JSON line to events.jsonl, reload, verify seq
    continues monotonic from the last GOOD line."""
    s1 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s1.start()
    try:
        await s1.open("s", app_id="a", user_id="u")
        for i in range(5):
            await s1.append_event(
                "s", Event(type="user_message", content=f"m{i}"),
            )
        await s1.flusher.flush()
    finally:
        await s1.stop()

    jsonl = s1._session_dir("s") / "events.jsonl"
    with jsonl.open("a", encoding="utf-8") as f:
        f.write('{"type": "user_message", "seq": 6, "incompl')

    s2 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s2.start()
    try:
        state = await s2.open("s", app_id="a", user_id="u")
        assert state.last_seq == 5
        next_seq = await s2.append_event(
            "s", Event(type="user_message", content="resumed"),
        )
        assert next_seq == 6
    finally:
        await s2.stop()


@pytest.mark.asyncio
async def test_meta_corrupt_falls_back_to_jsonl_tail(tmp_root: Path):
    """meta.json was overwritten with garbage mid-flush. Reload still
    finds the right last_seq via the jsonl-tail fallback."""
    s1 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s1.start()
    try:
        await s1.open("s", app_id="a", user_id="u")
        for _ in range(8):
            await s1.append_event("s", Event(type="user_message", content="x"))
        await s1.flusher.flush()
    finally:
        await s1.stop()

    meta = s1._session_dir("s") / "meta.json"
    meta.write_text("not json", encoding="utf-8")

    s2 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s2.start()
    try:
        state = await s2.open("s", app_id="a", user_id="u")
        assert state.last_seq == 8
        seq = await s2.append_event("s", Event(type="user_message", content="y"))
        assert seq == 9
    finally:
        await s2.stop()


@pytest.mark.asyncio
async def test_snapshot_corrupt_replays_from_events(tmp_root: Path):
    """snapshot.json corrupted, but compaction.json + events.jsonl
    are intact. Reload falls back to replaying events post-cutoff
    (projections empty since snapshot is unreadable)."""
    s1 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s1.start()
    try:
        await s1.open("s", app_id="a", user_id="u")
        for i in range(10):
            await s1.append_event(
                "s", Event(type="user_message", content=f"m{i}"),
            )
        await s1.compact_session(
            "s", cutoff_seq=5, summary="recap",
            tokens_estimate=10, model="m",
        )
    finally:
        await s1.stop()

    snap_path = s1._session_dir("s") / "snapshot.json"
    snap_path.write_text("garbage{{{", encoding="utf-8")

    s2 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s2.start()
    try:
        state = await s2.open("s", app_id="a", user_id="u")
        assert state.applied_compaction is not None
        assert state.applied_compaction.cutoff_seq == 5
        assert state.messages[0].role == "system"
        assert "recap" in state.messages[0].content
        assert all(ev.seq > 5 for ev in state.events)
    finally:
        await s2.stop()


@pytest.mark.asyncio
async def test_compaction_without_snapshot_recovers(tmp_root: Path):
    """Simulate the gap between compaction.json write and
    snapshot.json write -- snapshot is missing, but reload still
    produces a coherent state."""
    s1 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s1.start()
    try:
        await s1.open("s", app_id="a", user_id="u")
        for i in range(6):
            await s1.append_event(
                "s", Event(type="user_message", content=f"m{i}"),
            )
        await s1.flusher.flush()
    finally:
        await s1.stop()

    sd = s1._session_dir("s")
    write_compaction(sd, Compaction(
        cutoff_seq=3, summary="manual recap",
        strategy="summary", key_events=[],
        created_at="2026-05-08T10:00:00+00:00",
        tokens_estimate=20, model="m",
    ))

    s2 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s2.start()
    try:
        state = await s2.open("s", app_id="a", user_id="u")
        assert state.applied_compaction is not None
        assert state.applied_compaction.cutoff_seq == 3
        assert "manual recap" in state.messages[0].content
        assert all(ev.seq > 3 for ev in state.events)
    finally:
        await s2.stop()


@pytest.mark.asyncio
async def test_no_flush_lost_on_close(tmp_root: Path):
    """Graceful close (store.stop) drains the queue; zero events
    lost. Replay should yield exactly what was appended."""
    s1 = InMemorySessionStore(root=tmp_root, flush_interval_ms=200)
    await s1.start()
    expected = []
    try:
        await s1.open("s", app_id="a", user_id="u")
        for i in range(50):
            seq = await s1.append_event(
                "s", Event(type="user_message", content=f"m{i}"),
            )
            expected.append(seq)
    finally:
        await s1.stop()

    s2 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s2.start()
    try:
        state = await s2.open("s", app_id="a", user_id="u")
        loaded_seqs = [ev.seq for ev in state.events]
        assert loaded_seqs == expected
    finally:
        await s2.stop()


@pytest.mark.asyncio
async def test_missing_session_dir_recovers_as_new(tmp_root: Path):
    """If the session dir was deleted between writes (operator rm),
    a fresh open() with create_if_missing=True starts a new session
    -- we don't hallucinate the past."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        state = await store.open("ghost", app_id="a", user_id="u")
        assert state.last_seq == 0
        assert state.events == []
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_double_flush_idempotent(tmp_root: Path):
    """Calling flush twice must be safe (no torn writes, no extra
    rows)."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        for i in range(5):
            await store.append_event(
                "s", Event(type="user_message", content=f"m{i}"),
            )
        await store.flusher.flush()
        await store.flusher.flush()
        jsonl = store._session_dir("s") / "events.jsonl"
        lines = jsonl.read_text().strip().splitlines()
        assert len(lines) == 5
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_index_corrupt_recovered_from_summaries(tmp_root: Path):
    """If the SQLite index file is wiped (operator rm idx.db) the
    rebuild_from_summaries entry point can repopulate it from the
    per-session meta.json files."""
    from digitorn.core.runtime.session_store.session_index import (
        SessionSummary, SqliteSessionIndex,
    )
    idx = SqliteSessionIndex(db_path=tmp_root / "idx.db")
    store = InMemorySessionStore(
        root=tmp_root / "sessions", flush_interval_ms=10, index=idx,
    )
    await store.start()
    try:
        for i in range(3):
            sid = f"s{i}"
            await store.open(sid, app_id="a", user_id="u")
            await store.append_event(sid, Event(type="user_message", content="x"))
            await store.close_session(sid)
        assert await idx.count_for_user("u") == 3
    finally:
        await store.stop()

    (tmp_root / "idx.db").unlink()
    idx2 = SqliteSessionIndex(db_path=tmp_root / "idx.db")
    assert await idx2.count_for_user("u") == 0

    summaries = []
    for i in range(3):
        sid = f"s{i}"
        store2 = InMemorySessionStore(
            root=tmp_root / "sessions", flush_interval_ms=10,
        )
        await store2.start()
        state = await store2.open(sid, app_id="a", user_id="u")
        summaries.append(SessionSummary.from_state_summary(state.summary()))
        await store2.stop()
    n = await idx2.rebuild_from_summaries(summaries)
    assert n == 3
    assert await idx2.count_for_user("u") == 3
