"""Async queue + drainer for agent-run tracking events."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from digitorn.core.runtime.run_tracker.protocols import TrackerBackend

logger = logging.getLogger(__name__)


_queue: Optional[asyncio.Queue[dict[str, Any]]] = None
_worker_task: Optional[asyncio.Task[None]] = None
_backend: Optional[TrackerBackend] = None
# Per-run sequence counter: {run_id: next_sequence}.
# Cleared on complete_run + on TTL eviction (5 min after last event).
_run_sequences: dict[str, int] = {}

MAX_QUEUE_SIZE = 10_000
DRAIN_TIMEOUT_SECONDS = 5.0


async def install_and_start(backend: TrackerBackend) -> None:
    """Bind a backend and start the drainer task. Idempotent."""
    global _backend, _queue, _worker_task
    if _worker_task is not None and not _worker_task.done():
        return  # already running

    await backend.setup()
    _backend = backend
    _queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
    _worker_task = asyncio.create_task(_drain_loop(), name="run_tracker_worker")
    logger.info(
        "run_tracker started backend=%s max_queue=%d",
        type(backend).__name__, MAX_QUEUE_SIZE,
    )


async def stop() -> None:
    """Drain the queue then cancel the worker. Called at shutdown."""
    global _worker_task, _queue, _backend
    if _worker_task is None:
        return
    if _queue is not None:
        try:
            await asyncio.wait_for(_queue.join(), timeout=DRAIN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                "run_tracker drain timeout - %d items dropped",
                _queue.qsize(),
            )
    _worker_task.cancel()
    try:
        await _worker_task
    except asyncio.CancelledError:
        pass
    if _backend is not None:
        try:
            await _backend.teardown()
        except Exception as exc:
            logger.warning("run_tracker backend teardown failed: %s", exc)
    _worker_task = None
    _queue = None
    _backend = None
    _run_sequences.clear()


def is_running() -> bool:
    return _worker_task is not None and not _worker_task.done()


def enqueue(kind: str, payload: dict[str, Any]) -> bool:
    """Add one event to the worker queue"""
    if _queue is None:
        return False
    item = {"kind": kind, "payload": payload}
    try:
        _queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        logger.warning(
            "run_tracker queue full (%d) - dropping kind=%s",
            MAX_QUEUE_SIZE, kind,
        )
        return False


def allocate_sequence(run_id: str) -> int:
    """Reserve the next sequence number for `run_id`. Called by the"""
    next_seq = _run_sequences.get(run_id, 0) + 1
    _run_sequences[run_id] = next_seq
    return next_seq


def forget_run(run_id: str) -> None:
    """Drop the per-run sequence counter once the run is complete."""
    _run_sequences.pop(run_id, None)


async def _drain_loop() -> None:
    while True:
        item = await _queue.get()
        try:
            await _dispatch(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "run_tracker drain failed kind=%s: %s",
                item.get("kind"), exc, exc_info=True,
            )
        finally:
            _queue.task_done()


async def _dispatch(item: dict[str, Any]) -> None:
    kind = item["kind"]
    payload = item["payload"]
    backend = _backend
    if backend is None:
        return  # backend was uninstalled mid-flight; drop silently

    if kind == "start":
        await backend.start_run(**payload)
    elif kind == "complete":
        await backend.complete_run(**payload)
        # Run terminated: drop the per-run state.
        forget_run(payload.get("run_id", ""))
    elif kind == "event":
        await backend.emit_event(**payload)
    elif kind == "inc_turns":
        await backend.increment_turns(**payload)
    elif kind == "inc_subs":
        await backend.increment_sub_agents(**payload)
    else:
        logger.warning("run_tracker unknown event kind=%s", kind)
