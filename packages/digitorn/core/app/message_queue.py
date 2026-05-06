"""Per-session message queue - FIFO persistent backlog of user messages.

When a client sends a message while a turn is already running, we enqueue
it here instead of failing. A dispatcher (implemented as a post-turn
trigger in ``manager.chat``) pulls the head of the queue as soon as the
current turn finishes.

This module is a **facade**. Concrete storage is selected at boot via
``configure_backend()`` based on ``settings.session.queue.backend``:

- ``sql``    - Postgres/SQLite-backed (default, durable, audit).
- ``redis``  - Redis Lua-backed (atomic mark_done+drain - closes the
                 race that can leave a row orphaned in the queue between
                 a turn finishing and the next ``next_queued`` scan).
- ``memory`` - process-local dict (tests, fallback when Redis unreachable).

Public API kept intentionally identical to the pre-facade module so
callers do not change. In-process state (`_awaiters`, `_session_locks`)
stays at the module level - it was never persistent.

Contract:

- ``enqueue()`` - always persists; returns the row.
- ``next_queued()`` - picks the head for a session, marks it ``running``.
- ``mark_done() / mark_failed() / mark_cancelled()`` - status transitions.
- ``finish_and_drain()`` - atomic terminal-flip + pop next (Redis only;
   on SQL it falls back to mark_done() then next_queued() - slightly
   racy but identical to today's behaviour).
- ``cancel()``  - removes a queued message before it runs.
- ``list_for_session()`` - snapshot for GET /queue.
- ``rehydrate()`` - resets stuck ``running`` rows back to ``queued`` at
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
_TERMINAL = ("completed", "failed", "cancelled")


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


class QueueFullError(Exception):
    """Raised when enqueueing would exceed ``session.queue.max_depth``."""

    def __init__(self, msg: str, *, depth: int, max_depth: int) -> None:
        super().__init__(msg)
        self.depth = depth
        self.max_depth = max_depth


# ─── Process-local state (NOT persisted, intentionally) ──────────────
# ``_awaiters``: correlation_id → Future resolved when the message
# finishes (mode=wait path). The Future's result is the turn's final
# output dict.
# ``_session_locks``: dict guard for FIFO enqueue ordering per session
# in the SQL backend.
_awaiters: dict[str, asyncio.Future] = {}
_session_locks: dict[str, asyncio.Lock] = {}
# queue_row_id -> raw bearer captured at enqueue time. Lets the drain
# worker re-publish the inbound JWT ContextVar before dispatch, so a
# gateway-routed turn picked up later still authenticates.
_pending_jwts: dict[str, str] = {}


def attach_jwt(row_id: str, token: str | None) -> None:
    """Stash the inbound JWT for a queued row (no-op on empty token)."""
    if row_id and token:
        _pending_jwts[row_id] = token


def pop_jwt(row_id: str) -> str | None:
    """Pop the JWT stashed by ``attach_jwt`` (or ``None`` when absent)."""
    if not row_id:
        return None
    return _pending_jwts.pop(row_id, None)


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


# ════════════════════════════════════════════════════════════════════
# Memory Backend (fallback when Redis unreachable, also used by tests)
# ════════════════════════════════════════════════════════════════════


class MemoryQueueBackend:
    """In-process dict-backed queue. State is per-process, lost on
    restart - intended for dev (no local Redis required) and tests.

    Public API matches ``RedisQueueBackend`` so the facade can swap
    transparently. NOT thread-safe; relies on the daemon's single
    asyncio loop convention.
    """

    def __init__(self) -> None:
        # session_id → list[dict] (queued rows in FIFO order)
        self._queued: dict[str, list[dict]] = {}
        # session_id → dict (the running row, or None)
        self._running: dict[str, dict] = {}
        # row_id → row dict (cross-index)
        self._by_id: dict[str, dict] = {}
        self._next_position: dict[str, int] = {}

    def _alloc_position(self, session_id: str) -> int:
        n = self._next_position.get(session_id, 0) + 1
        self._next_position[session_id] = n
        return n

    def _row_to_entry(self, row: dict) -> QueueEntry:
        return QueueEntry(
            id=row["id"], app_id=row["app_id"], session_id=row["session_id"],
            user_id=row.get("user_id", ""), position=row["position"],
            message=row.get("message", ""),
            image_refs=list(row.get("image_refs") or []),
            status=row["status"],
            correlation_id=row.get("correlation_id", ""),
            enqueued_at=row.get("enqueued_at", time.time()),
            started_at=row.get("started_at"),
            finished_at=row.get("finished_at"),
            error_code=row.get("error_code", ""),
        )

    async def enqueue(
        self, *, app_id, session_id, user_id, message,
        image_refs=None, ttl_seconds=3600, max_depth=20,
    ) -> QueueEntry:
        depth = len(self._queued.get(session_id, []))
        if session_id in self._running:
            depth += 1
        if depth >= max_depth:
            raise QueueFullError(
                f"Queue at capacity ({depth}/{max_depth}) for session {session_id}",
                depth=depth, max_depth=max_depth,
            )
        row_id = uuid.uuid4().hex
        correlation_id = f"fp-{uuid.uuid4().hex[:12]}"
        position = self._alloc_position(session_id)
        row = {
            "id": row_id, "app_id": app_id, "session_id": session_id,
            "user_id": user_id or "", "position": position,
            "message": message or "",
            "image_refs": list(image_refs or []),
            "status": "queued", "correlation_id": correlation_id,
            "enqueued_at": time.time(),
            "ttl_expires_at": time.time() + ttl_seconds,
        }
        self._queued.setdefault(session_id, []).append(row)
        self._by_id[row_id] = row
        return self._row_to_entry(row)

    async def merge_or_enqueue(
        self, *, app_id, session_id, user_id, message,
        image_refs=None, window_seconds=2.0,
        separator="\n\n---\n\n", ttl_seconds=3600, max_depth=20,
    ) -> tuple[QueueEntry, bool]:
        q = self._queued.get(session_id, [])
        if q:
            tail = q[-1]
            if (
                tail["status"] == "queued"
                and tail["user_id"] == (user_id or "")
                and (time.time() - tail["enqueued_at"]) <= window_seconds
            ):
                tail["message"] = (tail["message"] or "").rstrip() + separator + (message or "").lstrip()
                if image_refs:
                    tail["image_refs"] = list(tail["image_refs"]) + list(image_refs)
                tail["enqueued_at"] = time.time()
                return self._row_to_entry(tail), True
        entry = await self.enqueue(
            app_id=app_id, session_id=session_id, user_id=user_id,
            message=message, image_refs=image_refs,
            ttl_seconds=ttl_seconds, max_depth=max_depth,
        )
        return entry, False

    async def replace_last_or_enqueue(
        self, *, app_id, session_id, user_id, message,
        image_refs=None, ttl_seconds=3600, max_depth=20,
    ) -> tuple[QueueEntry, bool]:
        q = self._queued.get(session_id, [])
        if q:
            tail = q[-1]
            if tail["status"] == "queued" and tail["user_id"] == (user_id or ""):
                old_corr = tail["correlation_id"]
                tail["message"] = message or ""
                tail["image_refs"] = list(image_refs or [])
                tail["correlation_id"] = f"fp-{uuid.uuid4().hex[:12]}"
                tail["enqueued_at"] = time.time()
                fail_awaiter(old_corr, RuntimeError("replaced by new message"))
                return self._row_to_entry(tail), True
        entry = await self.enqueue(
            app_id=app_id, session_id=session_id, user_id=user_id,
            message=message, image_refs=image_refs,
            ttl_seconds=ttl_seconds, max_depth=max_depth,
        )
        return entry, False

    async def next_queued(
        self, session_id, lease_seconds=_DEFAULT_LEASE_SECONDS,
    ) -> QueueEntry | None:
        if session_id in self._running:
            return None
        q = self._queued.get(session_id, [])
        # TTL sweep.
        now = time.time()
        i = 0
        while i < len(q):
            if q[i].get("ttl_expires_at", 0) < now:
                q[i]["status"] = "failed"
                q[i]["error_code"] = "queue_ttl_expired"
                q[i]["finished_at"] = now
                q.pop(i)
                continue
            i += 1
        if not q:
            return None
        row = q.pop(0)
        row["status"] = "running"
        row["started_at"] = now
        row["worker_id"] = _WORKER_ID
        row["lease_until"] = now + lease_seconds
        self._running[session_id] = row
        return self._row_to_entry(row)

    async def heartbeat(self, row_id, lease_seconds=_DEFAULT_LEASE_SECONDS):
        row = self._by_id.get(row_id)
        if row is None or row["status"] != "running":
            return False
        row["lease_until"] = time.time() + lease_seconds
        return True

    async def reap_expired_leases(self) -> int:
        n = 0
        now = time.time()
        for sid, row in list(self._running.items()):
            if (row.get("lease_until") or 0) < now:
                row["status"] = "queued"
                row["started_at"] = None
                row["lease_until"] = None
                row["worker_id"] = ""
                self._queued.setdefault(sid, []).insert(0, row)
                del self._running[sid]
                n += 1
        return n

    async def mark_done(self, row_id):
        await self._set_status(row_id, "completed")

    async def mark_failed(self, row_id, error_code=""):
        await self._set_status(row_id, "failed", error_code=error_code)

    async def mark_cancelled(self, row_id):
        await self._set_status(row_id, "cancelled")

    async def finish_and_drain(
        self, session_id, row_id, terminal_status="completed",
        error_code="", lease_seconds=_DEFAULT_LEASE_SECONDS,
    ) -> QueueEntry | None:
        await self._set_status(row_id, terminal_status, error_code=error_code)
        return await self.next_queued(session_id, lease_seconds=lease_seconds)

    async def _set_status(self, row_id, status, error_code=""):
        row = self._by_id.get(row_id)
        if row is None or row["status"] in _TERMINAL:
            return
        row["status"] = status
        row["error_code"] = error_code or ""
        row["finished_at"] = time.time()
        # If this row was running, clear the per-session running slot.
        sid = row["session_id"]
        if self._running.get(sid) is row:
            del self._running[sid]
        # Also remove from queued list if it was there.
        q = self._queued.get(sid, [])
        if row in q:
            q.remove(row)

    async def cancel(self, session_id, row_id) -> bool:
        row = self._by_id.get(row_id)
        if row is None or row["session_id"] != session_id:
            return False
        if row["status"] != "queued":
            return False
        row["status"] = "cancelled"
        row["finished_at"] = time.time()
        q = self._queued.get(session_id, [])
        if row in q:
            q.remove(row)
        return True

    async def clear(self, session_id) -> int:
        q = self._queued.get(session_id, [])
        n = len(q)
        for row in q:
            row["status"] = "cancelled"
            row["finished_at"] = time.time()
        self._queued[session_id] = []
        return n

    async def list_for_session(
        self, session_id, include_finished=False,
    ) -> list[QueueEntry]:
        out: list[QueueEntry] = []
        for row in self._queued.get(session_id, []):
            out.append(self._row_to_entry(row))
        running = self._running.get(session_id)
        if running:
            out.append(self._row_to_entry(running))
        if not include_finished:
            out = [e for e in out if e.status in ("queued", "running")]
        out.sort(key=lambda e: e.position)
        return out

    async def rehydrate_on_boot(self) -> int:
        # Memory backend has nothing to recover - process restart wipes state.
        return 0

    async def sessions_with_queued(self):
        return [
            (rows[0]["app_id"], sid, rows[0]["user_id"])
            for sid, rows in self._queued.items() if rows
        ]

    async def purge_queued_on_boot(self, reason="daemon_restart") -> int:
        n = 0
        for sid in list(self._queued):
            for row in self._queued[sid]:
                row["status"] = "cancelled"
                row["error_code"] = reason
                row["finished_at"] = time.time()
                n += 1
            self._queued[sid] = []
        return n

    async def has_running(self, session_id) -> bool:
        return session_id in self._running

    async def depth_for_session(self, session_id) -> int:
        n = len(self._queued.get(session_id, []))
        if session_id in self._running:
            n += 1
        return n


# ════════════════════════════════════════════════════════════════════
# Backend selection
# ════════════════════════════════════════════════════════════════════


_active_backend: Any = None


def configure_backend(backend: Any) -> None:
    """Install the active backend instance. Called at daemon boot.

    Tests may also call this directly to inject a MemoryQueueBackend.
    """
    global _active_backend
    _active_backend = backend
    logger.info("queue_backend_configured kind=%s", type(backend).__name__)


def _get_backend() -> Any:
    """Return the active backend, defaulting to ``MemoryQueueBackend``
    when nothing has been configured. Lazy default keeps tests and
    pre-``configure_backend`` callers working without ceremony.
    """
    global _active_backend
    if _active_backend is None:
        _active_backend = MemoryQueueBackend()
        logger.info(
            "queue_backend_default_lazy kind=MemoryQueueBackend "
            "(call configure_backend at boot to override)"
        )
    return _active_backend


def get_backend() -> Any:
    """Public read accessor - exposed for diagnostics / tests."""
    return _get_backend()


async def setup_from_settings() -> None:
    """Boot-time setup. Reads ``settings.session.queue.backend`` and
    constructs the requested backend.

    * Production: ``backend="redis"`` is the canonical choice. The
      daemon refuses to start with Redis unreachable (no silent
      fallback) - operators must fix the Redis URL or set
      ``backend="memory"`` explicitly for ephemeral runs.
    * Dev / tests: ``backend="memory"`` keeps things ultra-simple
      with no external service. State is lost on restart.

    SQL is no longer a supported backend; the only durable option
    is Redis, which already serves as the daemon's KV / cron / preview
    bus. Keeping a second persistence path was dead weight.
    """
    try:
        from digitorn.core.config import get_settings
        cfg = get_settings().session.queue
    except Exception as exc:
        logger.warning(
            "queue_setup: settings unavailable, defaulting to memory: %s", exc,
        )
        configure_backend(MemoryQueueBackend())
        return

    kind = (cfg.backend or "redis").lower()

    if kind == "memory":
        configure_backend(MemoryQueueBackend())
        return

    if kind == "redis":
        url = cfg.redis_url
        if not url:
            # Reuse server.kv_backend if it points at Redis.
            try:
                from digitorn.core.config import get_settings as _gs
                kv = _gs().server.kv_backend or ""
                if kv.startswith(("redis://", "rediss://")):
                    url = kv
            except Exception:
                pass
        if not url:
            raise RuntimeError(
                "queue_setup: backend=redis but no redis_url configured. "
                "Set DIGITORN_SESSION__QUEUE__REDIS_URL or "
                "DIGITORN_SERVER__KV_BACKEND to a redis:// URL, or set "
                "DIGITORN_SESSION__QUEUE__BACKEND=memory for dev.",
            )
        from digitorn.core.app.queue_redis import (
            create_redis_client, RedisQueueBackend,
        )
        client = await create_redis_client(url)
        if client is None:
            raise RuntimeError(
                f"queue_setup: redis client creation failed for {url!r}. "
                "Verify Redis is reachable, or set "
                "DIGITORN_SESSION__QUEUE__BACKEND=memory for dev.",
            )
        configure_backend(RedisQueueBackend(client))
        return

    raise RuntimeError(
        f"queue_setup: unknown backend kind={kind!r}; "
        "expected 'redis' or 'memory'.",
    )


# ════════════════════════════════════════════════════════════════════
# Public module-level API - thin dispatchers preserved for backward compat
# ════════════════════════════════════════════════════════════════════


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
    return await _get_backend().enqueue(
        app_id=app_id, session_id=session_id, user_id=user_id,
        message=message, image_refs=image_refs,
        ttl_seconds=ttl_seconds, max_depth=max_depth,
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
    return await _get_backend().merge_or_enqueue(
        app_id=app_id, session_id=session_id, user_id=user_id,
        message=message, image_refs=image_refs,
        window_seconds=window_seconds, separator=separator,
        ttl_seconds=ttl_seconds, max_depth=max_depth,
    )


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
    return await _get_backend().replace_last_or_enqueue(
        app_id=app_id, session_id=session_id, user_id=user_id,
        message=message, image_refs=image_refs,
        ttl_seconds=ttl_seconds, max_depth=max_depth,
    )


async def next_queued(
    session_id: str, lease_seconds: int = _DEFAULT_LEASE_SECONDS,
) -> QueueEntry | None:
    return await _get_backend().next_queued(session_id, lease_seconds=lease_seconds)


async def heartbeat(
    row_id: str, lease_seconds: int = _DEFAULT_LEASE_SECONDS,
) -> bool:
    return await _get_backend().heartbeat(row_id, lease_seconds=lease_seconds)


async def reap_expired_leases() -> int:
    return await _get_backend().reap_expired_leases()


async def mark_done(row_id: str) -> None:
    await _get_backend().mark_done(row_id)


async def mark_failed(row_id: str, error_code: str = "") -> None:
    await _get_backend().mark_failed(row_id, error_code=error_code)


async def mark_cancelled(row_id: str) -> None:
    await _get_backend().mark_cancelled(row_id)


async def finish_and_drain(
    session_id: str,
    row_id: str,
    terminal_status: str = "completed",
    error_code: str = "",
    lease_seconds: int = _DEFAULT_LEASE_SECONDS,
) -> QueueEntry | None:
    """Atomically transition ``row_id`` to ``terminal_status`` AND pop
    the next queued row for the session. The Redis backend wraps both
    steps in a single Lua script so a concurrent ``enqueue`` cannot
    sneak between them. The SQL backend falls back to two sequential
    queries (same race as the original ``mark_done`` then
    ``next_queued`` flow - preserved deliberately for drop-in compat).
    """
    return await _get_backend().finish_and_drain(
        session_id, row_id, terminal_status=terminal_status,
        error_code=error_code, lease_seconds=lease_seconds,
    )


async def cancel(session_id: str, row_id: str) -> bool:
    return await _get_backend().cancel(session_id, row_id)


async def clear(session_id: str) -> int:
    return await _get_backend().clear(session_id)


async def list_for_session(
    session_id: str, include_finished: bool = False,
) -> list[QueueEntry]:
    return await _get_backend().list_for_session(
        session_id, include_finished=include_finished,
    )


async def rehydrate_on_boot() -> int:
    return await _get_backend().rehydrate_on_boot()


async def sessions_with_queued() -> list[tuple[str, str, str]]:
    return await _get_backend().sessions_with_queued()


async def purge_queued_on_boot(reason: str = "daemon_restart") -> int:
    return await _get_backend().purge_queued_on_boot(reason=reason)


async def has_running(session_id: str) -> bool:
    return await _get_backend().has_running(session_id)


async def depth_for_session(session_id: str) -> int:
    return await _get_backend().depth_for_session(session_id)
