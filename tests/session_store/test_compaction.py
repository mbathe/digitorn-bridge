"""Compaction: cursor + summary + frontend refresh event."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.compaction import (
    Compaction, read_compaction, write_compaction,
)
from digitorn.core.runtime.session_store.store import InMemorySessionStore
from digitorn.core.runtime.session_store.types import Event


def test_compaction_roundtrip(tmp_root: Path):
    sd = tmp_root / "s"
    comp = Compaction(
        cutoff_seq=42, summary="user did stuff",
        strategy="summary_plus_keys",
        key_events=[{"seq": 10, "type": "tool_result"}],
        created_at="2026-05-08T00:00:00+00:00",
        tokens_estimate=1234,
        model="claude-opus-4-7",
    )
    write_compaction(sd, comp)
    loaded = read_compaction(sd)
    assert loaded is not None
    assert loaded.cutoff_seq == 42
    assert loaded.summary == "user did stuff"
    assert loaded.strategy == "summary_plus_keys"
    assert loaded.tokens_estimate == 1234
    assert loaded.model == "claude-opus-4-7"
    assert loaded.key_events == [{"seq": 10, "type": "tool_result"}]


def test_compaction_missing_returns_none(tmp_root: Path):
    assert read_compaction(tmp_root / "doesnotexist") is None


def test_compaction_corrupt_returns_none(tmp_root: Path):
    sd = tmp_root / "s"
    sd.mkdir()
    (sd / "compaction.json").write_text("not json", encoding="utf-8")
    assert read_compaction(sd) is None


def test_compaction_overwrites(tmp_root: Path):
    sd = tmp_root / "s"
    comp1 = Compaction(
        cutoff_seq=10, summary="first", strategy="summary",
        key_events=[], created_at="t1",
        tokens_estimate=100, model="m",
    )
    comp2 = Compaction(
        cutoff_seq=50, summary="second", strategy="summary",
        key_events=[], created_at="t2",
        tokens_estimate=500, model="m",
    )
    write_compaction(sd, comp1)
    write_compaction(sd, comp2)
    loaded = read_compaction(sd)
    assert loaded.cutoff_seq == 50
    assert loaded.summary == "second"


@pytest.mark.asyncio
async def test_compact_session_drops_ram_keeps_disk(tmp_root: Path):
    """compact_session drops events with seq <= cutoff from RAM but
    leaves events.jsonl untouched. The frontend ``stream_full_history``
    still sees all events."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        for i in range(10):
            await store.append_event("s", Event(
                type="user_message", role="user", content=f"m{i}",
            ))
        await store.flusher.flush()

        comp = await store.compact_session(
            "s",
            cutoff_seq=5,
            summary="user said hi 5 times",
            strategy="summary",
            tokens_estimate=42,
            model="claude-opus-4-7",
        )

        state = store.state("s")
        assert state.applied_compaction is not None
        assert state.applied_compaction.cutoff_seq == 5

        ram_seqs = [ev.seq for ev in state.events]
        assert all(s > 5 for s in ram_seqs)
        assert min(ram_seqs) == 6
        assert state.messages[0].role == "system"
        assert "user said hi 5 times" in state.messages[0].content
        assert all(m.seq > 5 for m in state.messages[1:] if m.role != "system")

        full = []
        async for ev in store.stream_full_history("s"):
            full.append(ev.seq)
        live_message_seqs = [s for s in full if s <= 10]
        assert sorted(live_message_seqs) == list(range(1, 11))
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_compact_session_emits_compact_done_event(tmp_root: Path):
    """compact_session appends a compact_done event with the new
    seq + the full new context payload."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        for i in range(5):
            await store.append_event("s", Event(
                type="user_message", role="user", content=f"m{i}",
            ))

        seq_before = store.state("s").last_seq
        await store.compact_session(
            "s",
            cutoff_seq=3,
            summary="recap",
            tokens_estimate=10,
            model="claude-opus-4-7",
        )

        state = store.state("s")
        compact_events = [e for e in state.events if e.type == "compact_done"]
        assert len(compact_events) == 1
        ce = compact_events[0]
        assert ce.seq == seq_before + 1
        assert ce.payload["cutoff_seq"] == 3
        assert ce.payload["summary"] == "recap"
        assert ce.payload["tokens_estimate"] == 10
        assert ce.payload["model"] == "claude-opus-4-7"
        ctx = ce.payload["context_after"]
        assert "messages" in ctx
        assert "todos" in ctx
        assert "memory_facts" in ctx
        assert "workspace_files" in ctx
        assert "tool_calls" in ctx
        assert "children" in ctx
        assert "blobs" in ctx
        assert "cost_total" in ctx
        assert "tokens_in" in ctx
        assert "tokens_out" in ctx
        assert ctx["first_seq"] >= 4
        assert ctx["messages"][0]["role"] == "system"
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_compact_session_persists_files(tmp_root: Path):
    """compact_session writes both compaction.json and snapshot.json."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        for i in range(5):
            await store.append_event("s", Event(
                type="user_message", role="user", content=f"m{i}",
            ))
        await store.append_event("s", Event(
            type="todo_add", payload={"id": "t1", "text": "ship"},
        ))
        await store.compact_session(
            "s", cutoff_seq=3, summary="recap",
            tokens_estimate=10, model="m",
        )
        sd = store._session_dir("s")
        assert (sd / "compaction.json").exists()
        assert (sd / "snapshot.json").exists()
        snap = json.loads((sd / "snapshot.json").read_text())
        assert len(snap["todos"]) == 1
        assert snap["todos"][0]["text"] == "ship"
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_reload_after_compaction_restores_state(tmp_root: Path):
    """Daemon restart: load session that was compacted. State has
    summary as system message, post-cutoff events, and full
    projections from snapshot."""
    s1 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s1.start()
    try:
        await s1.open("s", app_id="myapp", user_id="me")
        for i in range(10):
            await s1.append_event("s", Event(
                type="user_message", role="user", content=f"m{i}",
            ))
        await s1.append_event("s", Event(
            type="todo_add", payload={"id": "t1", "text": "ship"},
        ))
        await s1.append_event("s", Event(
            type="memory_remember", payload={"key": "k", "value": "v"},
        ))
        await s1.compact_session(
            "s", cutoff_seq=5, summary="early conv recap",
            tokens_estimate=20, model="m",
        )
    finally:
        await s1.stop()

    s2 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s2.start()
    try:
        state = await s2.open("s", app_id="myapp", user_id="me")
        assert state.applied_compaction is not None
        assert state.applied_compaction.cutoff_seq == 5
        assert state.messages[0].role == "system"
        assert "early conv recap" in state.messages[0].content
        assert all(ev.seq > 5 for ev in state.events)
        assert len(state.todos) == 1
        assert state.todos[0].text == "ship"
        assert state.memory_facts == {"k": "v"}
    finally:
        await s2.stop()


@pytest.mark.asyncio
async def test_compact_invalid_cutoff_raises(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        await store.append_event("s", Event(type="user_message", content="x"))
        with pytest.raises(ValueError, match="out of range"):
            await store.compact_session(
                "s", cutoff_seq=999, summary="s",
                tokens_estimate=0, model="m",
            )
        with pytest.raises(ValueError, match="out of range"):
            await store.compact_session(
                "s", cutoff_seq=-1, summary="s",
                tokens_estimate=0, model="m",
            )
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_compact_unopened_session_raises(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root)
    await store.start()
    try:
        with pytest.raises(KeyError, match="session_not_open"):
            await store.compact_session(
                "nope", cutoff_seq=1, summary="s",
                tokens_estimate=0, model="m",
            )
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_stream_full_history_ignores_compaction(tmp_root: Path):
    """The frontend / UI replay sees the FULL chronology even when
    the agent's RAM has been compacted to a smaller window."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        for i in range(20):
            await store.append_event("s", Event(
                type="user_message", role="user", content=f"m{i}",
            ))
        await store.compact_session(
            "s", cutoff_seq=10, summary="first half",
            tokens_estimate=15, model="m",
        )
        await store.flusher.flush()

        all_seqs = []
        async for ev in store.stream_full_history("s"):
            all_seqs.append(ev.seq)

        assert 1 in all_seqs
        assert 5 in all_seqs
        assert 10 in all_seqs
        assert 15 in all_seqs
        assert 20 in all_seqs
        assert 21 in all_seqs
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_double_compaction(tmp_root: Path):
    """Two compactions in a row: the second compaction's cutoff
    overwrites the first. compaction.json holds the LATEST."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        for i in range(20):
            await store.append_event("s", Event(
                type="user_message", role="user", content=f"m{i}",
            ))

        await store.compact_session(
            "s", cutoff_seq=5, summary="first compaction",
            tokens_estimate=10, model="m",
        )
        await store.compact_session(
            "s", cutoff_seq=15, summary="second compaction",
            tokens_estimate=20, model="m",
        )

        state = store.state("s")
        assert state.applied_compaction.cutoff_seq == 15
        assert "second compaction" in state.messages[0].content
        assert all(
            ev.seq > 15 for ev in state.events if ev.type != "compact_done"
        )
    finally:
        await store.stop()
