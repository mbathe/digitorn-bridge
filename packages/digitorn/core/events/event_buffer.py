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
        # Track last access time per session_id too - session-scope
        # counters live in ``_seq`` under ``session::<uuid>`` keys and
        # need their own TTL eviction; otherwise the dict grows by one
        # entry per session ever touched for the daemon's lifetime.
        self._session_last_access: dict[str, float] = {}
        self._last_eviction: float = 0.0
        # Per-user lock on seq increment - without this, two concurrent
        # publishers (streaming token + memory_update + approval) raced
        # through `_seq[uid] + 1` and both got the same value back.
        # `threading.Lock` rather than `asyncio.Lock` because publishers
        # call from threadpool workers too (subprocess, DB).
        import threading as _th
        self._seq_lock = _th.Lock()

    @staticmethod
    def _scope_key(*, user_id: str, session_id: str | None) -> str:
        """Shared scope rule: session-scope drops user_id (UUIDs unique),
        user-scope falls back. Mirrored 1:1 in
        ``SeqAllocator._scope_key`` -- keep both in sync.
        """
        return f"session::{session_id}" if session_id else f"user::{user_id}"

    def bump_to(self, *, user_id: str = "", session_id: str | None = None, value: int) -> None:
        """Forward-only push of the seq high-water mark for a scope.

        Used to back-propagate seqs allocated by ANOTHER allocator
        (notably ``SessionStore.SeqAllocator`` when it appends an
        internal event like ``compact_done`` or ``agent_spawn``).
        Without this, the next ``next_seq`` call on the wire could
        return a number already burned on disk, surfacing as a
        duplicate seq on the client's timeline.

        Idempotent and forward-only: never lowers the counter. Safe
        to call concurrently -- guarded by ``_seq_lock``.
        """
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
        """Read the persisted ``last_seq`` for a session from the
        SessionStore (the post-Phase-4c source of truth).

        Returns the MAX of two sources:
          1. in-memory ``state.last_seq`` if loaded (may be stale if
             ``_load_or_create`` ran but events weren't fully replayed,
             or if the state was freshly initialised at 0 before disk
             load completed)
          2. ``meta.json["last_seq"]`` on disk (kept current by the
             DiskFlusher after every batch write -- this is what the
             flusher will compare against in ``_write_session`` and
             use to reject regressions)

        Taking the max guarantees the seq we return will not collide
        with anything already persisted. Using ONLY in-memory or ONLY
        disk would each miss the other source's high-water mark and
        trigger ``seq_regression`` cascades in the flusher.

        Safe to call from inside ``next_seq`` -- it doesn't touch the
        EventBuffer's lock and doesn't acquire any async lock. The
        ``meta.json`` read is sync but tiny (<1 KB JSON).
        """
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
        except Exception:
            pass

        disk_seq = 0
        try:
            session_dir = store._session_dir(session_id)  # type: ignore[attr-defined]
            from digitorn.core.runtime.session_store.meta_io import MetaIO
            meta = MetaIO.read(session_dir)
            if meta:
                disk_seq = int(meta.get("last_seq") or 0)
        except Exception:
            pass

        return max(mem_seq, disk_seq)

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
        # Session-scoped events key ONLY on session_id. Previously the
        # key was ``user_id::session_id`` - but ``socketio_bus._envelope``
        # calls ``append(user_id="", session_id=X)`` for module-level
        # ``UniversalEvent`` publications (the bus bridges the two pipes
        # so module events render in the chat identically to SessionEvents).
        # That produced scope ``"::X"`` while every SessionEvent for the
        # same session used ``"real_user::X"`` - two parallel counters,
        # collision-prone on the wire.
        # Sessions ids are UUIDs (globally unique), so dropping user_id
        # from the session-scope key is safe AND merges both pipes onto
        # the same counter. User-scope (inbox / approvals, no session_id)
        # still keys on user_id as before.
        scope_key = self._scope_key(user_id=user_id, session_id=session_id)
        with self._seq_lock:
            if scope_key not in self._seq:
                # Seed from the DB row that matches this EXACT scope. If
                # we seeded every session from ``MAX(seq)`` across the
                # user, another session's concurrent emit could steal
                # the resumed session's next seq and leave a gap -
                # "session A ended at 500, session B grabs 501, session A
                # resumes at 502" - which breaks the "seqs are successive
                # within a session" contract. Filter on session_id so
                # each session's counter continues exactly where it left
                # off across daemon restarts, session pauses, and every
                # other cold-start path.
                # Two-source seed (Phase-4c-aware):
                #   1. ``history_log`` table (legacy, may be empty if
                #      the daemon only writes to the SessionStore now)
                #   2. ``meta.json`` on disk via the SessionStore (the
                #      actual source of truth post Phase-4c)
                # Take the max so we never reuse a seq that's already
                # persisted in EITHER store. Without #2, a session
                # resumed after restart would seed at 0 from the DB
                # query and start re-emitting seq=1, 2, ... right on
                # top of the events already on disk -- causing the
                # ``seq_regression ... DROPPING duplicate`` cascade
                # in the disk flusher and total wire/disk divergence.
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
        """Return the max ``seq`` already persisted for this scope.

        When ``session_id`` is given, filter on it so a resumed session
        picks up exactly where it left off (per-session seq continuity
        after daemon restart / idle eviction / cold reconnect). Without
        the filter the seed would include every other session's seqs
        and the resumed session would skip numbers that belonged to its
        siblings - visible to the client as gaps in the ``seq`` series.

        Runs inline (blocking) in a fresh event loop via ``asyncio.run``
        if no loop is running, else schedules a task. Falls back to 0
        on DB init failure (CLI / sandbox / tests without DB).
        """
        try:
            from digitorn.core.database import get_session_factory
            from digitorn.core.models import HistoryLog
            from sqlalchemy import select, func
        except Exception:
            return 0

        async def _q() -> int:
            # Resolve the session factory INSIDE the coroutine so the
            # worker's contextvar override is in effect (otherwise we'd
            # capture the main daemon's asyncpg factory and end up with
            # a cross-loop connection - exactly the bug we're fixing).
            try:
                sf = get_session_factory()
            except RuntimeError:
                return 0
            # Unified ledger: events live in history_log with kind='event'.
            #
            # CRITICAL: the seed filter MUST mirror the in-memory scope
            # key used by ``next_seq`` (see line 85). The scope key is
            # ``session::<sid>`` when session_id is given (user_id
            # ignored - session UUIDs are globally unique), or
            # ``user::<uid>`` otherwise. Filtering the seed by user_id
            # AND session_id when only session_id is the live key yields
            # the wrong max for module-level events that publish with
            # ``user_id=""``: the seed query returns 0 because no real
            # event has user_id="", the counter restarts at 1, and the
            # new events collide with everything already persisted in
            # the same session.
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

        # Route through the persist worker so the query runs on its
        # dedicated psycopg3 loop. asyncpg used to die here with
        # ``InternalClientError: protocol state 3`` whenever the
        # spawned thread's fresh asyncio loop ended up sharing
        # connection state with the main daemon loop (cross-loop
        # contamination + PgBouncer rotation). The worker has its
        # own engine on its own loop, immune to both.
        try:
            from digitorn.core.runtime.persist_worker import get_default_worker
        except Exception:
            return 0
        try:
            worker = get_default_worker()
        except Exception:
            return 0

        # 3 attempts with a short backoff. Each attempt blocks the
        # caller for at most ``timeout`` seconds. Total worst-case
        # wall-clock: ~3 * 5s = 15s, but in practice the worker's
        # pooled connection completes in <50ms.
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

        # All retries failed. The caller will treat this as seed=0
        # and the UNIQUE INDEX on (session_id, seq) is the safety
        # net: any duplicate INSERT triggered by a wrong seed fails
        # at the DB layer instead of silently corrupting the log.
        import logging
        logging.getLogger(__name__).error(
            "event_seq_seed_unrecoverable scope=%s session=%s user=%s "
            "after %d attempts: %s",
            "session" if session_id else "user",
            session_id, user_id, 3, last_exc,
        )
        return 0

    def get_latest_seq(self, user_id: str, session_id: str | None = None) -> int:
        """Read the live counter for a scope WITHOUT incrementing it.

        Mirrors the scope-key logic of :meth:`next_seq`: ``session::<sid>``
        when ``session_id`` is given, ``user::<uid>`` otherwise. The
        previous implementation read ``self._seq.get(user_id, 0)`` which
        always returned 0 because the dict is keyed by the prefixed
        scope-key, never by the bare user_id - clients reconnecting on
        the strength of this number always saw ``latest_seq=0`` and
        triggered a full replay every single time.
        """
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
        """Stamp a new envelope and append it to the user's buffer.

        Returns the envelope (with ``seq`` and ``ts`` filled in).
        """
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

    def _drop_user_scopes(self, user_id: str) -> None:
        """Drop the per-user counter for this user. Session-scoped
        counters (keyed on ``session::<uuid>``) are independent of the
        user identity, so they're NOT removed here - they only get
        evicted via [clear_session] / [_evict_stale] when the session
        itself is inactive. A user disconnect doesn't invalidate an
        ongoing session's seq stream (another client can still be
        listening on the session room, e.g. background agent, external
        viewer).
        """
        self._seq.pop(f"user::{user_id}", None)

    def clear_user(self, user_id: str) -> None:
        self._buffers.pop(user_id, None)
        self._drop_user_scopes(user_id)
        self._last_access.pop(user_id, None)

    def _evict_stale(self, now: float | None = None) -> int:
        """Remove buffers + counters for users AND sessions inactive
        longer than _STALE_TTL. Safe: on next access the scope re-seeds
        from the DB at the correct max seq (per-session filter), so the
        counter resumes exactly where it left off.

        Returns the number of evicted users.
        """
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
