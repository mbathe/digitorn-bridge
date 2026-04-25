"""In-memory event buffer with monotonic seq + replay.

Thin, lock-free (per-user serialized via asyncio) ring buffer that
backs the Socket.IO event bus. Holds the last N envelopes per user so
clients reconnecting with ``?since=<seq>`` can catch up.

No persistence: on daemon restart the seq counter resets and buffers
are empty. Clients detect this via the ``latest_seq`` field in the
``connected`` handshake and can choose to do a full refresh.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

# Max envelopes kept per user. Enough for a mobile backgrounding cycle
# or a brief network blip without growing unbounded.
_USER_BUFFER_MAX = 2000

# Stale user eviction: buffers inactive for longer than this are removed
# to prevent unbounded memory growth with many distinct users.
_STALE_TTL = 1800  # 30 minutes

# Minimum interval between automatic eviction sweeps (seconds).
# Prevents eviction from running on every append() call.
_EVICT_INTERVAL = 60


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventBuffer:
    """Per-user ring buffer with monotonic seq and filtered replay."""

    def __init__(self, max_per_user: int = _USER_BUFFER_MAX) -> None:
        self._max = max_per_user
        self._seq: dict[str, int] = {}
        self._buffers: dict[str, deque[dict[str, Any]]] = {}
        # Track last access time per user for stale eviction
        self._last_access: dict[str, float] = {}
        self._last_eviction: float = 0.0
        # Per-user lock on seq increment — without this, two concurrent
        # publishers (streaming token + memory_update + approval) raced
        # through `_seq[uid] + 1` and both got the same value back.
        # `threading.Lock` rather than `asyncio.Lock` because publishers
        # call from threadpool workers too (subprocess, DB).
        import threading as _th
        self._seq_lock = _th.Lock()

    def next_seq(self, user_id: str, session_id: str | None = None) -> int:
        """Generate the next monotonic seq.

        Per-session when `session_id` is given (the ordering key Flutter
        needs to dedup + reorder events within a single chat view),
        per-user otherwise (global inbox / approvals).

        On first access, seed from the DB so we never recycle sequence
        numbers that already exist in ``session_events``. This guarantees
        strict monotonicity across restarts.

        Thread-safe: the read-increment-write is guarded by `_seq_lock`
        so concurrent publishers can't both read the same value.
        """
        scope_key = f"{user_id}::{session_id}" if session_id else user_id
        with self._seq_lock:
            if scope_key not in self._seq:
                self._seq[scope_key] = self._load_seed_from_db(user_id)
            n = self._seq[scope_key] + 1
            self._seq[scope_key] = n
            return n

    @staticmethod
    def _load_seed_from_db(user_id: str) -> int:
        """Return the max ``seq`` in session_events for this user.

        Runs inline (blocking) in a fresh event loop via ``asyncio.run``
        if no loop is running, else schedules a task. Falls back to 0
        on DB init failure (CLI / sandbox / tests without DB).
        """
        try:
            from digitorn.core.database import get_session_factory
            from digitorn.core.models import SessionEvent
            from sqlalchemy import select, func
        except Exception:
            return 0
        try:
            sf = get_session_factory()
        except RuntimeError:
            return 0

        import asyncio as _aio

        async def _q() -> int:
            async with sf() as db:
                r = await db.execute(
                    select(func.max(SessionEvent.seq))
                    .where(SessionEvent.user_id == (user_id or ""))
                )
                return int(r.scalar() or 0)

        try:
            loop = _aio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            # We're inside a running loop — fetch synchronously via a
            # short helper that spins a thread to await.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(_aio.run, _q())
                try:
                    return fut.result(timeout=5.0)
                except Exception:
                    return 0
        try:
            return _aio.run(_q())
        except Exception:
            return 0

    def get_latest_seq(self, user_id: str) -> int:
        return self._seq.get(user_id, 0)

    def append(
        self,
        *,
        user_id: str,
        type: str,
        kind: str,
        payload: dict[str, Any] | None,
        app_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Stamp a new envelope and append it to the user's buffer.

        Returns the envelope (with ``seq`` and ``ts`` filled in).
        """
        now = time.monotonic()
        self._last_access[user_id] = now

        # Periodic stale eviction — at most once per _EVICT_INTERVAL
        if now - self._last_eviction > _EVICT_INTERVAL:
            self._evict_stale(now)
            self._last_eviction = now

        seq = self.next_seq(user_id, session_id)
        envelope: dict[str, Any] = {
            "type": type,
            "seq": seq,
            "kind": kind,
            "app_id": app_id,
            "session_id": session_id,
            "payload": payload or {},
            "ts": _utc_iso(),
        }
        buf = self._buffers.setdefault(user_id, deque(maxlen=self._max))
        buf.append(envelope)
        return envelope

    def replay(
        self,
        user_id: str,
        since: int,
        *,
        app_id: str | None = None,
        session_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return buffered envelopes with ``seq > since``, newest filters first.

        Filters by app_id / session_id when provided (session is more
        specific than app). Capped at ``limit`` envelopes.
        """
        # Touch access time on replay so active users don't get evicted
        self._last_access[user_id] = time.monotonic()

        buf = self._buffers.get(user_id)
        if not buf:
            return []
        out: list[dict[str, Any]] = []
        for env in buf:
            if env.get("seq", 0) <= since:
                continue
            if session_id and env.get("session_id") != session_id:
                continue
            if app_id and env.get("app_id") != app_id:
                continue
            out.append(env)
            if len(out) >= limit:
                break
        return out

    def clear_user(self, user_id: str) -> None:
        self._buffers.pop(user_id, None)
        self._seq.pop(user_id, None)
        self._last_access.pop(user_id, None)

    def _evict_stale(self, now: float | None = None) -> int:
        """Remove buffers for users inactive longer than _STALE_TTL.

        Returns the number of evicted users.
        """
        if now is None:
            now = time.monotonic()
        stale = [
            uid for uid, ts in self._last_access.items()
            if now - ts > _STALE_TTL
        ]
        for uid in stale:
            self._buffers.pop(uid, None)
            self._seq.pop(uid, None)
            self._last_access.pop(uid, None)
        return len(stale)

    @property
    def active_users(self) -> int:
        """Number of users with active buffers."""
        return len(self._buffers)
