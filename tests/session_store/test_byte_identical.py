"""Byte-identical reconstruction: the events written + reloaded must
be IDENTICAL, field by field, to the input events. This is the
contract that makes the system a drop-in replacement for the legacy
Postgres history_log."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.store import InMemorySessionStore
from digitorn.core.runtime.session_store.types import Event


@pytest.mark.asyncio
async def test_event_roundtrip_byte_identical(tmp_root: Path):
    """Write 100 events with all 25 columns populated. Reload from
    disk. Compare every field of every event."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    written: list[Event] = []
    try:
        await store.open("s", app_id="myapp", user_id="me")
        for i in range(100):
            ev = Event(
                type=f"type_{i % 5}",
                kind="event",
                role="user" if i % 2 else "assistant",
                content=f"content {i}",
                tool_call_id=f"tc-{i}" if i % 3 == 0 else None,
                tool_calls=[{"id": f"x{i}", "name": "t"}] if i % 4 == 0 else None,
                name=f"name-{i}" if i % 5 == 0 else None,
                payload={"i": i, "nested": {"k": "v" * (i % 7)}},
                before={"prev": i - 1},
                after={"next": i + 1},
                target_user_id=f"u{i}" if i % 3 else None,
                target_app_id=f"a{i}" if i % 4 else None,
                target_resource=f"r{i}" if i % 5 else None,
                ip_address="127.0.0.1",
                user_agent="test/1.0",
                correlation_id=f"corr-{i}",
                actor_user_id="actor",
                actor_roles=["admin", "user"],
                success=(i % 7 != 0),
                message=f"msg {i}" if i % 3 else "",
            )
            await store.append_event("s", ev)
            written.append(ev)
        await store.flusher.flush()
    finally:
        await store.stop()

    s2 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s2.start()
    try:
        state = await s2.open("s", app_id="myapp", user_id="me")
        loaded = state.events
        assert len(loaded) == len(written)
        for orig, reloaded in zip(written, loaded):
            assert orig.to_dict() == reloaded.to_dict(), (
                f"mismatch on seq={orig.seq}: "
                f"orig={orig.to_dict()} vs reloaded={reloaded.to_dict()}"
            )
    finally:
        await s2.stop()


@pytest.mark.asyncio
async def test_jsonl_format_stable(tmp_root: Path):
    """Each line of events.jsonl is a valid JSON dict with the 25
    expected keys (kind, type, ts, seq, ... message)."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        await store.append_event(
            "s", Event(type="user_message", role="user", content="hi"),
        )
        await store.flusher.flush()
    finally:
        await store.stop()

    path = list((tmp_root).rglob("events.jsonl"))[0]
    raw = path.read_text(encoding="utf-8").strip()
    line = json.loads(raw)
    expected_keys = {
        "type", "seq", "ts", "kind", "app_id", "session_id", "user_id",
        "actor_user_id", "actor_roles", "role", "content", "tool_call_id",
        "tool_calls", "name", "payload", "before", "after", "target_user_id",
        "target_app_id", "target_resource", "ip_address", "user_agent",
        "correlation_id", "success", "message",
    }
    assert set(line.keys()) == expected_keys


@pytest.mark.asyncio
async def test_seq_unique_within_session(tmp_root: Path):
    """Across N writes to the same session, no duplicate seq, no
    gaps. This is the contract the frontend depends on."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        seqs = []
        for _ in range(500):
            seqs.append(await store.append_event("s", Event(type="x")))
        assert seqs == list(range(1, 501))
        assert len(set(seqs)) == 500
    finally:
        await store.stop()
