"""DiskFlusher: batched async writes, fsync, monotonicity, atomic meta."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.disk_flusher import DiskFlusher
from digitorn.core.runtime.session_store.meta_io import MetaIO
from digitorn.core.runtime.session_store.types import Event


def _resolver(root: Path):
    def resolve(sid: str) -> Path:
        return root / sid
    return resolve


@pytest.mark.asyncio
async def test_writes_events_in_seq_order(tmp_root: Path):
    flusher = DiskFlusher(
        session_dir_resolver=_resolver(tmp_root), flush_interval_ms=10,
    )
    await flusher.start()
    try:
        for i in range(1, 11):
            flusher.enqueue("s1", Event(type="x", seq=i))
        await asyncio.sleep(0.1)
        await flusher.flush()
    finally:
        await flusher.stop()
    path = tmp_root / "s1" / "events.jsonl"
    lines = [json.loads(ln) for ln in path.read_text().splitlines() if ln]
    assert [ln["seq"] for ln in lines] == list(range(1, 11))


@pytest.mark.asyncio
async def test_meta_last_seq_updated(tmp_root: Path):
    flusher = DiskFlusher(
        session_dir_resolver=_resolver(tmp_root), flush_interval_ms=10,
    )
    await flusher.start()
    try:
        for i in range(1, 6):
            flusher.enqueue("s1", Event(type="x", seq=i))
        await asyncio.sleep(0.1)
        await flusher.flush()
    finally:
        await flusher.stop()
    meta = MetaIO.read(tmp_root / "s1")
    assert meta is not None
    assert meta["last_seq"] == 5
    assert meta["first_seq"] == 1
    assert meta["event_count"] == 5


@pytest.mark.asyncio
async def test_seq_regression_dropped(tmp_root: Path):
    flusher = DiskFlusher(
        session_dir_resolver=_resolver(tmp_root), flush_interval_ms=10,
    )
    await flusher.start()
    try:
        flusher.enqueue("s1", Event(type="a", seq=1))
        flusher.enqueue("s1", Event(type="b", seq=2))
        await asyncio.sleep(0.1)
        await flusher.flush()
        flusher.enqueue("s1", Event(type="dup", seq=1))
        flusher.enqueue("s1", Event(type="c", seq=3))
        await asyncio.sleep(0.1)
        await flusher.flush()
    finally:
        await flusher.stop()
    path = tmp_root / "s1" / "events.jsonl"
    lines = [json.loads(ln) for ln in path.read_text().splitlines() if ln]
    seqs = [ln["seq"] for ln in lines]
    assert seqs == [1, 2, 3]
    types = [ln["type"] for ln in lines]
    assert "dup" not in types


@pytest.mark.asyncio
async def test_separate_sessions_isolated(tmp_root: Path):
    flusher = DiskFlusher(
        session_dir_resolver=_resolver(tmp_root), flush_interval_ms=10,
    )
    await flusher.start()
    try:
        flusher.enqueue("a", Event(type="x", seq=1))
        flusher.enqueue("b", Event(type="x", seq=1))
        flusher.enqueue("a", Event(type="x", seq=2))
        flusher.enqueue("b", Event(type="x", seq=2))
        await asyncio.sleep(0.1)
        await flusher.flush()
    finally:
        await flusher.stop()
    a_lines = (tmp_root / "a" / "events.jsonl").read_text().strip().splitlines()
    b_lines = (tmp_root / "b" / "events.jsonl").read_text().strip().splitlines()
    assert len(a_lines) == 2
    assert len(b_lines) == 2


@pytest.mark.asyncio
async def test_drain_on_stop(tmp_root: Path):
    flusher = DiskFlusher(
        session_dir_resolver=_resolver(tmp_root), flush_interval_ms=50,
    )
    await flusher.start()
    for i in range(1, 21):
        flusher.enqueue("s1", Event(type="x", seq=i))
    await flusher.stop()
    lines = (tmp_root / "s1" / "events.jsonl").read_text().strip().splitlines()
    assert len(lines) == 20


@pytest.mark.asyncio
async def test_flush_returns_when_drained(tmp_root: Path):
    flusher = DiskFlusher(
        session_dir_resolver=_resolver(tmp_root), flush_interval_ms=10,
    )
    await flusher.start()
    try:
        flusher.enqueue("s1", Event(type="x", seq=1))
        await flusher.flush()
        path = tmp_root / "s1" / "events.jsonl"
        assert path.exists()
        assert len(path.read_text().splitlines()) == 1
    finally:
        await flusher.stop()
