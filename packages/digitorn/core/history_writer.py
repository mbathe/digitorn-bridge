"""Batched, async-drained writer for the unified ``history_log`` table.

Motivation
----------
Every message, event, and audit row lands in ``history_log``. Under
load, the daemon publishes tens of events per turn per session. If we
``await`` each INSERT on the agent's critical path, the loop serialises
on SQLite's write lock and becomes slow.

The previous design wrapped each write in ``asyncio.create_task`` —
fire-and-forget. Fast per-event but:

* one DB transaction per event (SQLite commits are O(ms) each)
* hundreds of in-flight tasks under burst
* asyncio GC can silently cancel tasks if not pinned

This module replaces that with a **single-writer, batched-commit**
architecture:

1. ``enqueue(record_dict)`` is O(1) — dict is pre-stamped with ``ts``
   at enqueue time (so ordering matches the caller's perception) then
   pushed on a bounded ``asyncio.Queue``. **The caller never awaits.**
2. A single background task (``_run``) drains up to ``BATCH_MAX``
   records per cycle and commits them in ONE transaction. Short flush
   interval (``FLUSH_MS=50``) keeps the crash window tight.
3. On graceful daemon shutdown, ``stop()`` drains the queue before
   returning — **zero loss for controlled stops**.
4. Under queue overflow (huge burst), falls back to synchronous insert
   so writes can't silently vanish.

Crash window (kill -9):
  At most one batch of events is in-flight without an fsync. With
  FLUSH_MS=50 that's ≤50 ms of events in the worst case, vs >500 ms
  under the previous fire-and-forget design.

The writer is a process-singleton held under ``_WRITER``. Import
``record()`` from :mod:`digitorn.core.history` as before — it now
routes through the writer automatically.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


# Tuning constants — picked to keep the crash window small while
# amortising fsync across many inserts.
BATCH_MAX: int = 200
FLUSH_MS: int = 50
QUEUE_MAX: int = 100_000
# Maximum time the writer is allowed to spend draining on shutdown
# before we give up and surface a warning.
SHUTDOWN_DRAIN_TIMEOUT_S: float = 20.0


class HistoryWriter:
    """Single-writer batched persister for ``history_log``."""

    def __init__(
        self, *,
        batch_max: int = BATCH_MAX,
        flush_ms: int = FLUSH_MS,
        queue_max: int = QUEUE_MAX,
    ) -> None:
        self._batch_max = batch_max
        self._flush_s = flush_ms / 1000
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=queue_max,
        )
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # Counters exposed for diagnostics / metrics.
        self.dropped: int = 0
        self.written: int = 0
        self.batch_count: int = 0
        self.last_batch_size: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="history_writer")

    async def stop(self, *, timeout: float = SHUTDOWN_DRAIN_TIMEOUT_S) -> None:
        """Stop the writer — drains the queue before returning.

        Called from the daemon's shutdown handler BEFORE ``close_db``
        so the final writes land before the engine disappears.
        """
        self._stop.set()
        task = self._task
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(
                "history_writer_stop_timeout remaining=%d dropped=%d",
                self._queue.qsize(), self.dropped,
            )
            task.cancel()
            try:
                await task
            except Exception:
                pass
        finally:
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def qsize(self) -> int:
        return self._queue.qsize()

    # ── Producer side ─────────────────────────────────────────────

    def enqueue(self, record: dict[str, Any]) -> bool:
        """Non-blocking push. Returns True if queued, False if dropped.

        The caller's invocation path NEVER blocks on the DB — this is
        the whole point. Under queue overflow (huge burst), the drop
        is counted and logged; the caller can fall back to a sync
        insert if it cares.
        """
        try:
            self._queue.put_nowait(record)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped % 100 == 1:
                logger.warning(
                    "history_writer_queue_full dropped_total=%d qsize=%d",
                    self.dropped, self._queue.qsize(),
                )
            return False

    # ── Consumer loop ─────────────────────────────────────────────

    async def _run(self) -> None:
        """Drain loop: pull up to BATCH_MAX per cycle, commit in one tx."""
        try:
            while True:
                # Keep draining until the stop signal AND the queue
                # is empty. This guarantees graceful shutdown flushes
                # everything the producers enqueued.
                if self._stop.is_set() and self._queue.empty():
                    return
                batch: list[dict[str, Any]] = []
                try:
                    first = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=0.25,
                    )
                    batch.append(first)
                except asyncio.TimeoutError:
                    continue

                # Greedy drain up to the batch cap or time window.
                deadline = time.monotonic() + self._flush_s
                while (
                    len(batch) < self._batch_max
                    and time.monotonic() < deadline
                ):
                    try:
                        batch.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                await self._flush(batch)
        except asyncio.CancelledError:
            # Shutdown forced by timeout — best-effort flush of what's
            # left, then re-raise.
            remaining: list[dict[str, Any]] = []
            while True:
                try:
                    remaining.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if remaining:
                try:
                    await self._flush(remaining)
                except Exception as exc:
                    logger.error(
                        "history_writer_final_flush_failed size=%d: %s",
                        len(remaining), exc,
                    )
            raise
        except Exception as exc:
            logger.exception("history_writer_fatal: %s", exc)

    async def _flush(self, batch: list[dict[str, Any]]) -> None:
        """Commit one batch in a single transaction.

        Retries individual rows on ts-collision (very rare — the
        monotonic clock protects at enqueue time, but multi-process
        scenarios with a shared DB can still race).
        """
        if not batch:
            return
        try:
            from digitorn.core.database import get_session_factory
            from digitorn.core.models import HistoryLog
        except Exception as exc:
            logger.debug("history_writer_import_failed: %s", exc)
            return
        try:
            sf = get_session_factory()
        except RuntimeError:
            # DB not ready (unit tests / pre-init). Drop silently —
            # the caller's context doesn't expect persistence here.
            return

        from sqlalchemy.exc import IntegrityError

        try:
            async with sf() as db:
                for item in batch:
                    db.add(HistoryLog(**item))
                await db.commit()
            self.written += len(batch)
            self.batch_count += 1
            self.last_batch_size = len(batch)
        except IntegrityError:
            # Fall back to per-row with bump-on-collision. Rare.
            logger.info(
                "history_writer_batch_integrity_retry size=%d", len(batch),
            )
            for item in batch:
                await self._insert_one_with_retry(item)
        except Exception as exc:
            logger.exception(
                "history_writer_batch_failed size=%d: %s", len(batch), exc,
            )

    async def _insert_one_with_retry(
        self, item: dict[str, Any], max_retries: int = 3,
    ) -> None:
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import HistoryLog
        from digitorn.core.unique_clock import unique_utc_now
        from sqlalchemy.exc import IntegrityError
        try:
            sf = get_session_factory()
        except RuntimeError:
            return

        for attempt in range(max_retries + 1):
            try:
                async with sf() as db:
                    db.add(HistoryLog(**item))
                    await db.commit()
                self.written += 1
                return
            except IntegrityError:
                if attempt >= max_retries:
                    logger.error(
                        "history_writer_row_retry_exhausted type=%s",
                        item.get("type"),
                    )
                    return
                # Bump and retry with a fresh (strictly-greater) ts.
                item["ts"] = unique_utc_now()
            except Exception as exc:
                logger.exception(
                    "history_writer_row_failed type=%s: %s",
                    item.get("type"), exc,
                )
                return


# ── Process-singleton access ────────────────────────────────────────

_WRITER: HistoryWriter | None = None


def get_writer() -> HistoryWriter | None:
    """Return the process-singleton writer, or None if none is wired.

    When ``None``, callers fall back to a synchronous insert path so
    tests / CLI without a running daemon keep working.
    """
    return _WRITER


def set_writer(writer: HistoryWriter | None) -> None:
    global _WRITER
    _WRITER = writer


async def start_writer() -> HistoryWriter:
    """Instantiate + start the process-singleton writer."""
    global _WRITER
    if _WRITER is not None and _WRITER.running:
        return _WRITER
    w = HistoryWriter()
    await w.start()
    _WRITER = w
    logger.info(
        "history_writer_started batch_max=%d flush_ms=%d queue_max=%d",
        BATCH_MAX, FLUSH_MS, QUEUE_MAX,
    )
    return w


async def stop_writer(*, timeout: float = SHUTDOWN_DRAIN_TIMEOUT_S) -> None:
    global _WRITER
    if _WRITER is None:
        return
    try:
        remaining = _WRITER.qsize()
        await _WRITER.stop(timeout=timeout)
        logger.info(
            "history_writer_stopped written=%d batches=%d dropped=%d drained_queued=%d",
            _WRITER.written, _WRITER.batch_count,
            _WRITER.dropped, remaining,
        )
    finally:
        _WRITER = None


__all__ = [
    "HistoryWriter",
    "get_writer", "set_writer", "start_writer", "stop_writer",
    "BATCH_MAX", "FLUSH_MS", "QUEUE_MAX",
]
