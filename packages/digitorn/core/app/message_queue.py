"""Per-session message queue — FIFO persistent backlog of user messages.

When a client sends a message while a turn is already running, we enqueue
it here instead of failing. A dispatcher (implemented as a post-turn
trigger in ``manager.chat``) pulls the head of the queue as soon as the
current turn finishes.

The queue is backed by the ``session_message_queue`` DB table so it
survives daemon restart. In-memory asyncio.Futures track awaiters when
callers opted into ``queue_mode=wait`` (they want the result inline, not
async via SSE).

Contract:

- ``enqueue()`` — always persists; returns the row.
- ``next_queued()`` — picks the head for a session, marks it ``running``.
- ``mark_done() / mark_failed() / mark_cancelled()`` — status transitions.
- ``cancel()``  — removes a queued message before it runs.
- ``list_for_session()`` — snapshot for GET /queue.
- ``rehydrate()`` — resets stuck ``running`` rows back to ``queued`` at
  daemon boot (crash recovery).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_WORKER_ID = f"pid-{os.getpid()}"
_DEFAULT_LEASE_SECONDS = 120


@dataclass
class QueueEntry:
    """In-memory view of a queued message."""
    id: str
    app_id: str
    session_id: str
    user_id: str
    position: int
    message: str
    image_refs: list
    status: str
    correlation_id: str
    enqueued_at: float  # unix seconds
    started_at: float | None = None
    finished_at: float | None = None
    error_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "app_id": self.app_id,
            "session_id": self.session_id,
            "position": self.position,
            "message": self.message,
            "status": self.status,
            "correlation_id": self.correlation_id,
            "enqueued_at": self.enqueued_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error_code": self.error_code,
        }


# Process-local state
# -------------------
# ``_awaiters``: correlation_id → Future resolved when the message finishes
# (mode=wait path). The Future's result is the turn's final output dict.
#
# ``_session_locks``: dict guard for FIFO enqueue ordering per session.
_awaiters: dict[str, asyncio.Future] = {}
_session_locks: dict[str, asyncio.Lock] = {}


def _lock_for(session_id: str) -> asyncio.Lock:
    lock = _session_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[session_id] = lock
    return lock


def awaiter_future(correlation_id: str) -> asyncio.Future:
    """Register a Future to be resolved when the message finishes."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _awaiters[correlation_id] = fut
    return fut


def resolve_awaiter(correlation_id: str, result: Any) -> None:
    fut = _awaiters.pop(correlation_id, None)
    if fut and not fut.done():
        fut.set_result(result)


def fail_awaiter(correlation_id: str, exc: Exception) -> None:
    fut = _awaiters.pop(correlation_id, None)
    if fut and not fut.done():
        fut.set_exception(exc)


# DB operations
# -------------

async def _next_position(session, session_id: str) -> int:
    """Next position number for a session (monotonic within session)."""
    from digitorn.core.models import SessionMessageQueue
    from sqlalchemy import select, func
    r = await session.execute(
        select(func.max(SessionMessageQueue.position))
        .where(SessionMessageQueue.session_id == session_id)
    )
    current = r.scalar() or 0
    return int(current) + 1


async def enqueue(
    *,
    app_id: str,
    session_id: str,
    user_id: str,
    message: str,
    image_refs: list | None = None,
    ttl_seconds: int = 3600,
    max_depth: int = 20,
) -> QueueEntry:
    """Enqueue a message. Raises ``QueueFullError`` at cap.

    The row is persisted atomically. Returns the in-memory view.
    """
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import SessionMessageQueue
    from sqlalchemy import select, func

    sf = get_session_factory()
    # Use the same "fp-" prefix as the fast-path so clients can filter
    # by a single pattern regardless of which path handled the message.
    correlation_id = f"fp-{uuid.uuid4().hex[:12]}"
    row_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=ttl_seconds)

    async with _lock_for(session_id):
        async with sf() as db:
            async with db.begin():
                # Depth check — count only queued + running
                count = (await db.execute(
                    select(func.count(SessionMessageQueue.id))
                    .where(SessionMessageQueue.session_id == session_id)
                    .where(SessionMessageQueue.status.in_(("queued", "running")))
                )).scalar() or 0
                if count >= max_depth:
                    raise QueueFullError(
                        f"Queue at capacity ({count}/{max_depth}) for session {session_id}",
                        depth=count, max_depth=max_depth,
                    )
                position = await _next_position(db, session_id)
                row = SessionMessageQueue(
                    id=row_id, app_id=app_id, session_id=session_id,
                    user_id=user_id or "",
                    position=position,
                    message=message or "",
                    image_refs=list(image_refs or []),
                    status="queued",
                    correlation_id=correlation_id,
                    enqueued_at=now,
                    ttl_expires_at=expires,
                )
                db.add(row)

    logger.info(
        "queue_enqueue session=%s position=%d correlation_id=%s",
        session_id, position, correlation_id,
    )
    return QueueEntry(
        id=row_id, app_id=app_id, session_id=session_id, user_id=user_id or "",
        position=position, message=message, image_refs=list(image_refs or []),
        status="queued", correlation_id=correlation_id,
        enqueued_at=now.timestamp(),
    )


async def merge_or_enqueue(
    *,
    app_id: str,
    session_id: str,
    user_id: str,
    message: str,
    image_refs: list | None = None,
    window_seconds: float = 2.0,
    separator: str = "\n\n---\n\n",
    ttl_seconds: int = 3600,
    max_depth: int = 20,
) -> tuple[QueueEntry, bool]:
    """If the tail of the queue has a recent ``queued`` message from the
    same user, merge this content into it and return the same row.
    Otherwise fall through to ``enqueue()``.

    Returns ``(entry, merged)`` — ``merged=True`` means the caller's new
    message was appended to an existing row, saving one turn.

    Never merges into running / completed / cancelled rows. Never merges
    across users. Preserves the merged row's position + correlation_id
    so existing clients keep their tracking.
    """
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import SessionMessageQueue
    from sqlalchemy import select

    sf = get_session_factory()
    now = datetime.now(timezone.utc)

    async with _lock_for(session_id):
        async with sf() as db:
            async with db.begin():
                r = await db.execute(
                    select(SessionMessageQueue)
                    .where(SessionMessageQueue.session_id == session_id)
                    .where(SessionMessageQueue.status == "queued")
                    .where(SessionMessageQueue.user_id == (user_id or ""))
                    .order_by(SessionMessageQueue.position.desc())
                    .limit(1)
                )
                tail = r.scalar_one_or_none()
                if tail is not None:
                    # SQLite strips tz info on read — normalise both
                    # sides to UTC-aware before subtracting.
                    ts = tail.enqueued_at
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    age = (now - ts).total_seconds()
                    if age <= window_seconds:
                        # Merge into the tail row.
                        tail.message = (
                            (tail.message or "").rstrip()
                            + separator
                            + (message or "").lstrip()
                        )
                        if image_refs:
                            existing = list(tail.image_refs or [])
                            existing.extend(image_refs)
                            tail.image_refs = existing
                        tail.enqueued_at = now  # slide the window
                        merged_entry = QueueEntry(
                            id=tail.id, app_id=tail.app_id,
                            session_id=tail.session_id,
                            user_id=tail.user_id, position=tail.position,
                            message=tail.message,
                            image_refs=list(tail.image_refs or []),
                            status="queued",
                            correlation_id=tail.correlation_id,
                            enqueued_at=now.timestamp(),
                        )
                        logger.info(
                            "queue_merge session=%s position=%d age=%.1fs",
                            session_id, tail.position, age,
                        )
                        return merged_entry, True
    # Outside the lock — recursive-safe enqueue.
    entry = await enqueue(
        app_id=app_id, session_id=session_id, user_id=user_id,
        message=message, image_refs=image_refs,
        ttl_seconds=ttl_seconds, max_depth=max_depth,
    )
    return entry, False


async def replace_last_or_enqueue(
    *,
    app_id: str,
    session_id: str,
    user_id: str,
    message: str,
    image_refs: list | None = None,
    ttl_seconds: int = 3600,
    max_depth: int = 20,
) -> tuple[QueueEntry, bool]:
    """If the session has a ``queued`` message from the same user at
    the tail, overwrite it in place with the new payload. Otherwise
    enqueue a fresh row.

    Returns ``(entry, replaced)`` — ``replaced=True`` means we swapped
    an existing row. The row keeps its ``id`` + ``position`` but gets a
    fresh ``correlation_id`` so the client can distinguish between the
    pre- and post-replace message in its tracking map.

    Never touches a ``running`` row — once the dispatcher picks it up
    it's too late to replace.
    """
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import SessionMessageQueue
    from sqlalchemy import select

    sf = get_session_factory()
    now = datetime.now(timezone.utc)
    # BUG-010 + BUG-011: every other code path emits ``fp-<12hex>``
    # correlation ids. This one shipped ``<32hex>`` which mangled the
    # client's correlation tracking map (Turn 3 was mis-attributed to
    # Turn 1 when Turn 2 was blocked on an approval, because the ids
    # didn't share a format and the frontend fell back to the only
    # matching entry in its map). Stay on the ``fp-`` shape.
    new_correlation = f"fp-{uuid.uuid4().hex[:12]}"

    async with _lock_for(session_id):
        async with sf() as db:
            async with db.begin():
                r = await db.execute(
                    select(SessionMessageQueue)
                    .where(SessionMessageQueue.session_id == session_id)
                    .where(SessionMessageQueue.status == "queued")
                    .where(SessionMessageQueue.user_id == (user_id or ""))
                    .order_by(SessionMessageQueue.position.desc())
                    .limit(1)
                )
                tail = r.scalar_one_or_none()
                if tail is not None:
                    # Resolve any awaiter on the old correlation_id as
                    # cancelled — caller wanted to drop the old message.
                    old_corr = tail.correlation_id
                    tail.message = message or ""
                    tail.image_refs = list(image_refs or [])
                    tail.correlation_id = new_correlation
                    tail.enqueued_at = now
                    entry = QueueEntry(
                        id=tail.id, app_id=tail.app_id,
                        session_id=tail.session_id,
                        user_id=tail.user_id, position=tail.position,
                        message=tail.message,
                        image_refs=list(tail.image_refs or []),
                        status="queued",
                        correlation_id=tail.correlation_id,
                        enqueued_at=now.timestamp(),
                    )
                    # Drop any awaiter on the displaced message.
                    fail_awaiter(old_corr, RuntimeError("replaced by new message"))
                    logger.info(
                        "queue_replace_last session=%s position=%d",
                        session_id, tail.position,
                    )
                    return entry, True
    entry = await enqueue(
        app_id=app_id, session_id=session_id, user_id=user_id,
        message=message, image_refs=image_refs,
        ttl_seconds=ttl_seconds, max_depth=max_depth,
    )
    return entry, False


async def next_queued(
    session_id: str, lease_seconds: int = _DEFAULT_LEASE_SECONDS,
) -> QueueEntry | None:
    """Pick the head of the queue. Marks it ``running`` with a fresh lease."""
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import SessionMessageQueue
    from sqlalchemy import select, update

    sf = get_session_factory()
    async with sf() as db:
        async with db.begin():
            now = datetime.now(timezone.utc)
            await db.execute(
                update(SessionMessageQueue)
                .where(SessionMessageQueue.session_id == session_id)
                .where(SessionMessageQueue.status == "queued")
                .where(SessionMessageQueue.ttl_expires_at.isnot(None))
                .where(SessionMessageQueue.ttl_expires_at < now)
                .values(status="failed", error_code="queue_ttl_expired", finished_at=now)
            )

            r = await db.execute(
                select(SessionMessageQueue)
                .where(SessionMessageQueue.session_id == session_id)
                .where(SessionMessageQueue.status == "queued")
                .order_by(SessionMessageQueue.position.asc())
                .limit(1)
            )
            row = r.scalar_one_or_none()
            if row is None:
                return None
            row.status = "running"
            row.started_at = now
            row.lease_until = now + timedelta(seconds=lease_seconds)
            row.worker_id = _WORKER_ID
    return QueueEntry(
        id=row.id, app_id=row.app_id, session_id=row.session_id,
        user_id=row.user_id, position=row.position, message=row.message,
        image_refs=list(row.image_refs or []), status="running",
        correlation_id=row.correlation_id,
        enqueued_at=row.enqueued_at.timestamp(),
        started_at=(row.started_at.timestamp() if row.started_at else None),
    )


async def heartbeat(
    row_id: str, lease_seconds: int = _DEFAULT_LEASE_SECONDS,
) -> bool:
    """Extend the lease on a running row. Returns False if the row has been
    reaped (status != running or owned by another worker).
    """
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import SessionMessageQueue
    from sqlalchemy import update

    sf = get_session_factory()
    now = datetime.now(timezone.utc)
    async with sf() as db:
        async with db.begin():
            r = await db.execute(
                update(SessionMessageQueue)
                .where(SessionMessageQueue.id == row_id)
                .where(SessionMessageQueue.status == "running")
                .where(SessionMessageQueue.worker_id == _WORKER_ID)
                .values(lease_until=now + timedelta(seconds=lease_seconds))
            )
    return (r.rowcount or 0) > 0


async def reap_expired_leases() -> int:
    """Reset running rows whose lease has expired back to queued.
    Returns the number of reaped rows. Safe to call repeatedly.
    """
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import SessionMessageQueue
    from sqlalchemy import update

    sf = get_session_factory()
    now = datetime.now(timezone.utc)
    async with sf() as db:
        async with db.begin():
            r = await db.execute(
                update(SessionMessageQueue)
                .where(SessionMessageQueue.status == "running")
                .where(SessionMessageQueue.lease_until.isnot(None))
                .where(SessionMessageQueue.lease_until < now)
                .values(
                    status="queued",
                    started_at=None,
                    lease_until=None,
                    worker_id="",
                )
            )
    n = r.rowcount or 0
    if n:
        logger.warning("queue_reaper reset %d expired running rows to queued", n)
    return n


_TERMINAL = ("completed", "failed", "cancelled")


async def mark_done(row_id: str) -> None:
    await _set_status(row_id, "completed")


async def mark_failed(row_id: str, error_code: str = "") -> None:
    await _set_status(row_id, "failed", error_code=error_code)


async def mark_cancelled(row_id: str) -> None:
    await _set_status(row_id, "cancelled")


async def _set_status(row_id: str, status: str, error_code: str = "") -> None:
    """Set a row's status — but never move a row OUT of a terminal state.

    This is the key safety net for abort: when the user cancels mid-turn,
    the abort endpoint marks the row ``cancelled`` and ``_run_turn``'s
    ``finally`` later tries ``mark_done``. Without this guard the abort
    would be silently reverted. Terminal status is write-once.
    """
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import SessionMessageQueue
    from sqlalchemy import update

    sf = get_session_factory()
    async with sf() as db:
        async with db.begin():
            await db.execute(
                update(SessionMessageQueue)
                .where(SessionMessageQueue.id == row_id)
                .where(SessionMessageQueue.status.notin_(_TERMINAL))
                .values(
                    status=status,
                    error_code=error_code or "",
                    finished_at=datetime.now(timezone.utc),
                )
            )


async def cancel(session_id: str, row_id: str) -> bool:
    """Cancel a queued (non-running) message. Returns True if cancelled."""
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import SessionMessageQueue
    from sqlalchemy import select, update

    sf = get_session_factory()
    async with sf() as db:
        async with db.begin():
            r = await db.execute(
                select(SessionMessageQueue)
                .where(SessionMessageQueue.id == row_id)
                .where(SessionMessageQueue.session_id == session_id)
            )
            row = r.scalar_one_or_none()
            if row is None:
                return False
            if row.status != "queued":
                return False
            row.status = "cancelled"
            row.finished_at = datetime.now(timezone.utc)
    return True


async def clear(session_id: str) -> int:
    """Mark every queued (not running) message as cancelled. Returns count."""
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import SessionMessageQueue
    from sqlalchemy import update

    sf = get_session_factory()
    async with sf() as db:
        async with db.begin():
            r = await db.execute(
                update(SessionMessageQueue)
                .where(SessionMessageQueue.session_id == session_id)
                .where(SessionMessageQueue.status == "queued")
                .values(status="cancelled", finished_at=datetime.now(timezone.utc))
            )
    return r.rowcount or 0


async def list_for_session(
    session_id: str, include_finished: bool = False,
) -> list[QueueEntry]:
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import SessionMessageQueue
    from sqlalchemy import select

    sf = get_session_factory()
    async with sf() as db:
        stmt = (
            select(SessionMessageQueue)
            .where(SessionMessageQueue.session_id == session_id)
        )
        if not include_finished:
            stmt = stmt.where(
                SessionMessageQueue.status.in_(("queued", "running")),
            )
        stmt = stmt.order_by(SessionMessageQueue.position.asc())
        r = await db.execute(stmt)
        rows = r.scalars().all()
    return [
        QueueEntry(
            id=row.id, app_id=row.app_id, session_id=row.session_id,
            user_id=row.user_id, position=row.position, message=row.message,
            image_refs=list(row.image_refs or []), status=row.status,
            correlation_id=row.correlation_id,
            enqueued_at=row.enqueued_at.timestamp(),
            started_at=(row.started_at.timestamp() if row.started_at else None),
            finished_at=(row.finished_at.timestamp() if row.finished_at else None),
            error_code=row.error_code or "",
        )
        for row in rows
    ]


async def rehydrate_on_boot() -> int:
    """Reset rows left in ``running`` state (daemon crashed mid-turn)
    back to ``queued`` so the dispatcher picks them up again. Returns
    the number of rows rehydrated."""
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import SessionMessageQueue
    from sqlalchemy import update

    sf = get_session_factory()
    async with sf() as db:
        async with db.begin():
            r = await db.execute(
                update(SessionMessageQueue)
                .where(SessionMessageQueue.status == "running")
                .values(status="queued", started_at=None)
            )
    n = r.rowcount or 0
    if n:
        logger.info("queue_rehydrate reset %d stuck running rows → queued", n)
    return n


async def has_running(session_id: str) -> bool:
    """True if a message for this session is currently running WITH a valid lease.
    Expired-lease rows are treated as not-running (they'll be reaped to queued).
    """
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import SessionMessageQueue
    from sqlalchemy import select, func, or_

    sf = get_session_factory()
    now = datetime.now(timezone.utc)
    async with sf() as db:
        r = await db.execute(
            select(func.count(SessionMessageQueue.id))
            .where(SessionMessageQueue.session_id == session_id)
            .where(SessionMessageQueue.status == "running")
            .where(or_(
                SessionMessageQueue.lease_until.is_(None),
                SessionMessageQueue.lease_until >= now,
            ))
        )
    return int(r.scalar() or 0) > 0


async def depth_for_session(session_id: str) -> int:
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import SessionMessageQueue
    from sqlalchemy import select, func

    sf = get_session_factory()
    async with sf() as db:
        r = await db.execute(
            select(func.count(SessionMessageQueue.id))
            .where(SessionMessageQueue.session_id == session_id)
            .where(SessionMessageQueue.status.in_(("queued", "running")))
        )
    return int(r.scalar() or 0)


class QueueFullError(Exception):
    """Raised when enqueueing would exceed ``session.queue.max_depth``."""

    def __init__(self, msg: str, *, depth: int, max_depth: int) -> None:
        super().__init__(msg)
        self.depth = depth
        self.max_depth = max_depth
