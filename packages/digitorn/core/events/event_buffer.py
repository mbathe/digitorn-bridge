"""In-memory event buffer with monotonic seq + replay."""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

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
        self._last_access: dict[str, float] = {}
        self._session_last_access: dict[str, float] = {}
        self._last_eviction: float = 0.0
        # `threading.Lock` so threadpool publishers (subprocess, DB)
        # serialise through the same path as the loop.
        import threading as _th
        self._seq_lock = _th.Lock()

    @staticmethod
    def _scope_key(*, user_id: str, session_id: str | None) -> str:
        return f"session::{session_id}" if session_id else f"user::{user_id}"

    def bump_to(self, *, user_id: str = "", session_id: str | None = None, value: int) -> None:
        """Forward-only push of the seq high-water mark for a scope."""
        if value <= 0:
            return
        scope_key = self._scope_key(user_id=user_id, session_id=session_id)
        with self._seq_lock:
            current = self._seq.get(scope_key, 0)
            if value > current:
                self._seq[scope_key] = value

    @staticmethod
    def _load_seed_from_session_store(
        session_id: str | None = None,
    ) -> int:
        if not session_id:
            return 0
        try:
            from digitorn.core.runtime.session_store.bridge import (
                get_default_bridge,
            )
            bridge = get_default_bridge()
        except Exception:
            return 0
        if bridge is None:
            return 0
        try:
            store = bridge.store
        except AttributeError:
            return 0

        mem_seq = 0
        try:
            st = store.state(session_id) if hasattr(store, "state") else None
            if st is not None:
                mem_seq = int(getattr(st, "last_seq", 0) or 0)
        except Exception as exc:
            logger.debug("event_buffer mem seq read failed: %s", exc)

        disk_seq = 0
        try:
            session_dir = store._session_dir(session_id)  # type: ignore[attr-defined]
            from digitorn.core.runtime.session_store.meta_io import MetaIO
            meta = MetaIO.read(session_dir)
            if meta:
                disk_seq = int(meta.get("last_seq") or 0)
        except Exception as exc:
            logger.debug("event_buffer disk seq read failed: %s", exc)

        return max(mem_seq, disk_seq)

    def next_seq(self, user_id: str, session_id: str | None = None) -> int:
        """Generate the next monotonic seq."""
        scope_key = self._scope_key(user_id=user_id, session_id=session_id)
        with self._seq_lock:
            if scope_key not in self._seq:
                # Seed from MAX(history_log, SessionStore meta.json) for
                # this exact scope so a resumed session never reuses a
                # seq already persisted on disk.
                db_seed = self._load_seed_from_db(
                    user_id, session_id=session_id,
                )
                store_seed = self._load_seed_from_session_store(
                    session_id=session_id,
                )
                self._seq[scope_key] = max(db_seed, store_seed)
            n = self._seq[scope_key] + 1
            self._seq[scope_key] = n
            return n

    @staticmethod
    def _load_seed_from_db(
        user_id: str, *, session_id: str | None = None,
    ) -> int:
        try:
            from digitorn.core.runtime.session_store.bridge import (
                resolve_mode_from_env, BridgeMode,
            )
            if resolve_mode_from_env() == BridgeMode.PRIMARY:
                return 0
        except Exception as exc:
            logger.debug("event_buffer bridge mode probe failed: %s", exc)

        try:
            from digitorn.core.database import get_session_factory
            from digitorn.core.models import HistoryLog
            from sqlalchemy import select, func
        except Exception:
            return 0

        async def _q() -> int:
            # Resolve the factory inside the coroutine so the worker's
            # contextvar override is in effect (avoids cross-loop conns).
            try:
                sf = get_session_factory()
            except RuntimeError:
                return 0
            async with sf() as db:
                stmt = (
                    select(func.max(HistoryLog.seq))
                    .where(HistoryLog.kind == "event")
                )
                if session_id:
                    # Session scope: filter by session_id only.
                    stmt = stmt.where(HistoryLog.session_id == session_id)
                else:
                    # User scope: only user-global events (no session),
                    # otherwise we'd inherit a high session seq.
                    stmt = stmt.where(HistoryLog.user_id == (user_id or ""))
                    stmt = stmt.where(HistoryLog.session_id.is_(None))
                r = await db.execute(stmt)
                return int(r.scalar() or 0)

        # Run on the persist worker's dedicated psycopg3 loop to avoid asyncpg
        # cross-loop contamination under PgBouncer rotation.
        try:
            from digitorn.core.runtime.persist_worker import get_default_worker
        except Exception:
            return 0
        try:
            worker = get_default_worker()
        except Exception:
            return 0

        # 3 attempts with short backoff; worst case ~15s wall-clock.
        import time as _time
        last_exc: Exception | None = None
        for attempt, delay in enumerate((0.0, 0.3, 1.0)):
            if delay > 0:
                _time.sleep(delay)
            sentinel = object()
            try:
                result = worker.run_sync(_q, timeout=5.0, default=sentinel)
            except Exception as exc:
                last_exc = exc
                continue
            if result is not sentinel:
                return int(result or 0)

        # Retries exhausted; caller falls back to seed=0 and the unique
        # index on `(session_id, seq)` catches any duplicate insert.
        import logging
        logging.getLogger(__name__).error(
            "event_seq_seed_unrecoverable scope=%s session=%s user=%s "
            "after %d attempts: %s",
            "session" if session_id else "user",
            session_id, user_id, 3, last_exc,
        )
        return 0

    def get_latest_seq(self, user_id: str, session_id: str | None = None) -> int:
        """Read the live counter for a scope WITHOUT incrementing it."""
        scope_key = f"session::{session_id}" if session_id else f"user::{user_id}"
        return self._seq.get(scope_key, 0)

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
        """Stamp a new envelope and append it to the user's buffer."""
        now = time.monotonic()
        self._last_access[user_id] = now
        if session_id:
            self._session_last_access[session_id] = now

        # Periodic stale eviction - at most once per _EVICT_INTERVAL
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
        limit: int = 5000000,
    ) -> list[dict[str, Any]]:
        """Return buffered envelopes with `seq > since`, newest filters first."""
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

    def _drop_user_scopes(self, user_id: str) -> None:
        self._seq.pop(f"user::{user_id}", None)

    def clear_user(self, user_id: str) -> None:
        self._buffers.pop(user_id, None)
        self._drop_user_scopes(user_id)
        self._last_access.pop(user_id, None)

    def _evict_stale(self, now: float | None = None) -> int:
        if now is None:
            now = time.monotonic()
        stale_users = [
            uid for uid, ts in self._last_access.items()
            if now - ts > _STALE_TTL
        ]
        for uid in stale_users:
            self._buffers.pop(uid, None)
            self._drop_user_scopes(uid)
            self._last_access.pop(uid, None)

        stale_sessions = [
            sid for sid, ts in self._session_last_access.items()
            if now - ts > _STALE_TTL
        ]
        for sid in stale_sessions:
            self._seq.pop(f"session::{sid}", None)
            self._session_last_access.pop(sid, None)

        return len(stale_users)

    @property
    def active_users(self) -> int:
        """Number of users with active buffers."""
        return len(self._buffers)
