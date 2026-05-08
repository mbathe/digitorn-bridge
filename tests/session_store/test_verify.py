"""Byte-identical verification: diff helpers + dual-store comparison."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.store import InMemorySessionStore
from digitorn.core.runtime.session_store.types import Event
from digitorn.core.runtime.session_store.verify import (
    DiffReport, diff_event_lists, diff_jsonl_files,
    diff_stores_for_session,
)


def _ev(seq: int, **kw) -> Event:
    return Event(
        type=kw.pop("type", "user_message"),
        seq=seq,
        ts=kw.pop("ts", "2026-05-08T10:00:00+00:00"),
        kind=kw.pop("kind", "event"),
        **kw,
    )


def test_diff_empty_lists_equal():
    report = diff_event_lists([], [])
    assert report.equal is True
    assert report.event_count_left == 0


def test_diff_identical_lists():
    a = [_ev(1, content="hi"), _ev(2, content="hello")]
    b = [_ev(1, content="hi"), _ev(2, content="hello")]
    report = diff_event_lists(a, b)
    assert report.equal is True
    assert "EQUAL" in report.summary()


def test_diff_different_count():
    a = [_ev(1), _ev(2)]
    b = [_ev(1)]
    report = diff_event_lists(a, b)
    assert report.equal is False
    assert any("event_count" in d for d in report.field_diffs)


def test_diff_field_mismatch():
    a = [_ev(1, content="left")]
    b = [_ev(1, content="right")]
    report = diff_event_lists(a, b)
    assert report.equal is False
    assert any("content" in d for d in report.field_diffs)
    assert any("'left'" in d and "'right'" in d for d in report.field_diffs)


def test_diff_missing_seq_on_one_side():
    a = [_ev(1), _ev(2), _ev(3)]
    b = [_ev(1), _ev(3)]
    report = diff_event_lists(a, b)
    assert report.equal is False
    assert 2 in report.seq_misses


def test_diff_summary_truncates_long_diffs():
    a = [_ev(i, content=f"L{i}") for i in range(15)]
    b = [_ev(i, content=f"R{i}") for i in range(15)]
    report = diff_event_lists(a, b)
    assert report.equal is False
    text = report.summary()
    assert "and" in text and "more" in text


def test_diff_jsonl_files(tmp_root: Path):
    p1 = tmp_root / "a.jsonl"
    p2 = tmp_root / "b.jsonl"
    events = [
        {"type": "x", "seq": 1, "ts": "t1", "kind": "event"},
        {"type": "x", "seq": 2, "ts": "t2", "kind": "event"},
    ]
    p1.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    p2.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    report = diff_jsonl_files(p1, p2)
    assert report.equal is True


def test_diff_jsonl_files_skips_bad_lines(tmp_root: Path):
    p1 = tmp_root / "a.jsonl"
    p2 = tmp_root / "b.jsonl"
    p1.write_text(
        json.dumps({"type": "x", "seq": 1, "ts": "t", "kind": "event"})
        + "\nGARBAGE\n"
        + json.dumps({"type": "x", "seq": 2, "ts": "t", "kind": "event"}),
        encoding="utf-8",
    )
    p2.write_text(
        json.dumps({"type": "x", "seq": 1, "ts": "t", "kind": "event"})
        + "\n"
        + json.dumps({"type": "x", "seq": 2, "ts": "t", "kind": "event"}),
        encoding="utf-8",
    )
    report = diff_jsonl_files(p1, p2)
    assert report.equal is True


def test_diff_jsonl_missing_file(tmp_root: Path):
    p1 = tmp_root / "a.jsonl"
    p2 = tmp_root / "b.jsonl"
    report = diff_jsonl_files(p1, p2)
    assert report.equal is True
    assert report.event_count_left == 0
    assert report.event_count_right == 0


@pytest.mark.asyncio
async def test_dual_store_byte_identical(tmp_root: Path):
    """Run two SessionStores side by side over the same event stream;
    their event lists must match byte-for-byte."""
    left = InMemorySessionStore(
        root=tmp_root / "left", flush_interval_ms=10,
    )
    right = InMemorySessionStore(
        root=tmp_root / "right", flush_interval_ms=10,
    )
    await left.start()
    await right.start()
    try:
        for s in (left, right):
            await s.open("sid", app_id="a", user_id="u")
        for i in range(20):
            ev_left = Event(
                type="user_message", role="user", content=f"msg-{i}",
                payload={"i": i},
            )
            ev_right = Event(
                type="user_message", role="user", content=f"msg-{i}",
                payload={"i": i},
            )
            await left.append_event("sid", ev_left)
            await right.append_event("sid", ev_right)
        report = await diff_stores_for_session(
            left=left, right=right, sid="sid",
        )
        assert report.equal is True
        assert report.event_count_left == 20
    finally:
        await left.stop()
        await right.stop()


@pytest.mark.asyncio
async def test_dual_store_diff_detects_drift(tmp_root: Path):
    """Force a content drift between stores; the diff catches it."""
    left = InMemorySessionStore(
        root=tmp_root / "left", flush_interval_ms=10,
    )
    right = InMemorySessionStore(
        root=tmp_root / "right", flush_interval_ms=10,
    )
    await left.start()
    await right.start()
    try:
        for s in (left, right):
            await s.open("sid", app_id="a", user_id="u")
        await left.append_event(
            "sid", Event(type="user_message", content="left-content"),
        )
        await right.append_event(
            "sid", Event(type="user_message", content="right-content"),
        )
        report = await diff_stores_for_session(
            left=left, right=right, sid="sid",
        )
        assert report.equal is False
        assert any("content" in d for d in report.field_diffs)
    finally:
        await left.stop()
        await right.stop()


@pytest.mark.asyncio
async def test_dual_store_full_history_includes_compacted(tmp_root: Path):
    """include_compacted=True reads events.jsonl directly so a
    compacted left store still equals a never-compacted right store
    on the disk-level chronology."""
    left = InMemorySessionStore(
        root=tmp_root / "left", flush_interval_ms=10,
    )
    right = InMemorySessionStore(
        root=tmp_root / "right", flush_interval_ms=10,
    )
    await left.start()
    await right.start()
    try:
        for s in (left, right):
            await s.open("sid", app_id="a", user_id="u")
        for i in range(10):
            await left.append_event(
                "sid", Event(type="user_message", content=f"m{i}"),
            )
            await right.append_event(
                "sid", Event(type="user_message", content=f"m{i}"),
            )
        await left.compact_session(
            "sid", cutoff_seq=5, summary="recap",
            tokens_estimate=10, model="m",
        )
        await left.flusher.flush()
        await right.flusher.flush()
        report_full = await diff_stores_for_session(
            left=left, right=right, sid="sid",
            include_compacted=True,
        )
        full_seqs_left = {ev.seq for ev in (await _full_history(left, "sid"))}
        assert {1, 5, 10} <= full_seqs_left
        report_ram = await diff_stores_for_session(
            left=left, right=right, sid="sid",
        )
        assert report_ram.equal is False
    finally:
        await left.stop()
        await right.stop()


async def _full_history(store, sid):
    out = []
    async for ev in store.stream_full_history(sid):
        out.append(ev)
    return out
