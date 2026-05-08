"""Background write-behind worker.

Drains a bounded asyncio.Queue, groups events by session, and writes
each session's batch to ``events.jsonl`` with a single fsync per
session per flush cycle. Updates ``meta.json`` atomically after each
batch.

Single-writer-per-session: even though multiple producers can enqueue
concurrently, only the flusher coroutine ever opens a session's file.
That keeps the JSONL append truly append-only and the seq order on
disk identical to the seq order in memory.

Defense-in-depth: on every batch we sort by seq AND assert against
``meta.last_seq`` so a regression bug elsewhere can't silently corrupt
the journal. A regressed event is dropped + logged loudly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Callable

from digitorn.core.runtime.session_store.meta_io import MetaIO
from digitorn.core.runtime.session_store.types import Event, utc_iso

logger = logging.getLogger(__name__)


SessionDirResolver = Callable[[str], Path]


class DiskFlusher:
    """Async background worker. One instance per ``InMemorySessionStore``."""

    def __init__(
        self,
        *,
        session_dir_resolver: SessionDirResolver,
        flush_interval_ms: int = 50,
        batch_max: int = 200,
        queue_max: int = 100_000,
    ) -> None:
        self._dir_resolver = session_dir_resolver
        self._flush_s = flush_interval_ms / 1000.0
        self._batch_max = batch_max
        self._queue: asyncio.Queue[tuple[str, Event]] = asyncio.Queue(queue_max)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._drained = asyncio.Event()
        self._drained.set()

        self.dropped: int = 0
        self.written: int = 0
        self.batch_count: int = 0
        self.last_batch_size: int = 0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="session-disk-flusher")

    async def stop(self, *, drain_timeout: float = 20.0) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._drained.wait(), timeout=drain_timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "disk_flusher_drain_timeout queue_size=%d",
                self._queue.qsize(),
            )
        try:
            await asyncio.wait_for(self._task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None

    def enqueue(self, sid: str, event: Event) -> None:
        """Hot-path enqueue. ``put_nowait`` so callers never await
        on disk IO. Drops + logs if the queue is full (bounded).

        The queue size cap is the safety valve for "the daemon got
        wedged and disk IO can't keep up". In normal operation the
        flusher drains way faster than producers fill it.
        """
        try:
            self._queue.put_nowait((sid, event))
            self._drained.clear()
        except asyncio.QueueFull:
            self.dropped += 1
            logger.error(
                "disk_flusher_queue_full sid=%s seq=%d type=%s "
                "DROPPING event -- check disk IO health",
                sid, event.seq, event.type,
            )

    async def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            batch = await self._gather_batch()
            if not batch:
                if self._queue.empty():
                    self._drained.set()
                continue
            await self._write_batch(batch)
            self.batch_count += 1
            self.last_batch_size = sum(len(v) for v in batch.values())
            if self._queue.empty():
                self._drained.set()
        self._drained.set()

    async def _gather_batch(self) -> dict[str, list[Event]]:
        """Wait up to flush_s for the first event, then drain the
        queue non-blocking up to batch_max."""
        batch: dict[str, list[Event]] = defaultdict(list)
        try:
            sid, ev = await asyncio.wait_for(
                self._queue.get(), timeout=self._flush_s,
            )
            batch[sid].append(ev)
            self._queue.task_done()
        except asyncio.TimeoutError:
            return {}
        total = 1
        while total < self._batch_max and not self._queue.empty():
            try:
                sid, ev = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            batch[sid].append(ev)
            self._queue.task_done()
            total += 1
        return batch

    async def _write_batch(self, batch: dict[str, list[Event]]) -> None:
        """Write each session's events in parallel via to_thread.
        Each session is touched by exactly ONE thread for the duration
        of this batch (single-writer per session)."""
        await asyncio.gather(*[
            asyncio.to_thread(self._write_session, sid, evs)
            for sid, evs in batch.items()
        ])

    def _write_session(self, sid: str, events: list[Event]) -> None:
        events.sort(key=lambda e: e.seq)
        session_dir = self._dir_resolver(sid)
        session_dir.mkdir(parents=True, exist_ok=True)
        meta = MetaIO.read(session_dir) or {}
        last_seq = int(meta.get("last_seq", 0))
        kept: list[Event] = []
        for ev in events:
            if ev.seq <= last_seq:
                logger.error(
                    "seq_regression sid=%s on_disk=%d incoming=%d "
                    "type=%s -- DROPPING duplicate",
                    sid, last_seq, ev.seq, ev.type,
                )
                continue
            kept.append(ev)
            last_seq = ev.seq
        if not kept:
            return

        path = session_dir / "events.jsonl"
        with path.open("a", encoding="utf-8") as f:
            for ev in kept:
                f.write(json.dumps(ev.to_dict(), default=str, ensure_ascii=False))
                f.write("\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass

        meta.update({
            "session_id": sid,
            "last_seq": last_seq,
            "first_seq": int(meta.get("first_seq") or kept[0].seq),
            "event_count": int(meta.get("event_count", 0)) + len(kept),
            "last_flushed_at": utc_iso(),
        })
        if "started_at" not in meta:
            meta["started_at"] = kept[0].ts
        MetaIO.write(session_dir, meta)
        self.written += len(kept)

    async def flush(self) -> None:
        """Block until every queued event for every session is
        durably on disk. Used on graceful shutdown + before reads
        of partially-flushed sessions."""
        if self._queue.empty():
            return
        await self._drained.wait()
