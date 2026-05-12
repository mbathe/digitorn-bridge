"""InMemorySessionStore: orchestrates seq allocation, projections,
in-memory cache, and disk write-behind.

Single entry point for ``append_event``. The ordering of operations
inside ``append_event`` is the universal-truth invariant of the whole
subsystem:

  1. Allocate seq atomically (threading.Lock under the hood)
  2. Stamp ts
  3. Append to in-memory journal (state.events)
  4. Update live projections (state.messages, tool_calls, ...)
  5. Enqueue for disk flush
  6. Return seq to caller

Step 5 is awaited (in spirit -- ``put_nowait`` is sync but logically
ordered before the return). Callers MUST broadcast to clients only
AFTER ``append_event`` returns. That preserves the persist-before-
broadcast contract that the Postgres-based path enforces today.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from digitorn.core.runtime.session_store.compaction import (
    Compaction, read_compaction, write_compaction,
)
from digitorn.core.runtime.session_store.session_index import (
    SessionIndex, SessionSummary,
)
from digitorn.core.runtime.session_store.disk_flusher import DiskFlusher
from digitorn.core.runtime.session_store.meta_io import MetaIO
from digitorn.core.runtime.session_store.projections import apply_projection
from digitorn.core.runtime.session_store.seq_allocator import SeqAllocator
from digitorn.core.runtime.session_store.session_state import SessionState
from digitorn.core.runtime.session_store.snapshot import (
    build_snapshot, read_snapshot, write_snapshot,
)
from digitorn.core.runtime.session_store.types import (
    BlobRef, ChildAgentRef, Event, FileState, Message, ParentLink,
    Todo, ToolCall, ToolResult, utc_iso,
)

logger = logging.getLogger(__name__)


class InMemorySessionStore:
    """Process-wide in-memory store for active + recently-accessed
    sessions, with disk-backed durability."""

    def __init__(
        self,
        *,
        root: Path,
        flush_interval_ms: int = 50,
        max_sessions_in_memory: int = 1000,
        max_bytes_in_memory: int = 4 * 1024 * 1024 * 1024,
        index: SessionIndex | None = None,
        durability_mode: str = "relaxed",
        num_shards: int = 32,
        on_internal_seq_alloc: Any = None,
    ) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._sessions: "OrderedDict[str, SessionState]" = OrderedDict()
        self._sessions_lock = asyncio.Lock()
        self._max_sessions = max_sessions_in_memory
        self._max_bytes = max_bytes_in_memory
        self._current_bytes = 0
        self._index = index

        # Phase 2: per-session async lock map. The legacy SessionStore
        # exposed ``session_lock(app_id, sid, uid)`` so manager_v2 chat
        # turns serialise on the same session without blocking turns
        # in OTHER sessions. We replicate the contract here -- one
        # ``asyncio.Lock`` per session, lazy-allocated via
        # ``dict.setdefault`` (GIL-atomic, no separate meta lock
        # needed). Cleared on eviction / delete so the map stays
        # bounded by active session count.
        self._per_session_locks: dict[str, asyncio.Lock] = {}

        self._allocator = SeqAllocator(seed_loader=self._seed_seq_from_disk)
        # Hook called after an INTERNAL allocation (caller passed no
        # seq, store had to assign one). Lets the wire-side allocator
        # (EventBuffer) learn about the burned seq so its next
        # ``next_seq`` doesn't return the same value and produce a
        # duplicate. Signature: ``(session_id: str, seq: int) -> None``.
        # Wired in server.py once both bus and store are constructed.
        # ``None`` keeps the legacy behaviour for tests / standalone use.
        self._on_internal_seq_alloc = on_internal_seq_alloc
        self._flusher = DiskFlusher(
            session_dir_resolver=self._session_dir,
            chat_meta_resolver=self._chat_meta_for_flush,
            flush_interval_ms=flush_interval_ms,
            num_shards=num_shards,
            durability_mode=durability_mode,
        )

        # Phase 6: bg eviction worker. Hot path sets the signal; the
        # worker drains it. Replaces the per-append asyncio.create_task
        # which was wasteful (one task allocation per overflowing event).
        self._evict_signal = asyncio.Event()
        self._evict_task: asyncio.Task | None = None

        # Phase 6: bg snapshot worker. Periodically writes snapshot.json
        # for "ripe" sessions (>= SNAPSHOT_DELTA new events AND idle for
        # IDLE_THRESHOLD_S). Reduces cold-start reload time after crash.
        self._snapshot_task: asyncio.Task | None = None

        # Phase 6: latency ring buffer for ``append_event``. p50/p95/p99
        # exposed via ``stats()`` so ops can see hot-path health without
        # lighting up a profiler. Bounded so memory stays flat.
        self._append_durations_ms: deque[float] = deque(maxlen=1024)

    async def start(self) -> None:
        await self._flusher.start()
        if self._evict_task is None:
            self._evict_task = asyncio.create_task(
                self._run_eviction_worker(), name="session-evict-worker",
            )
        if self._snapshot_task is None:
            self._snapshot_task = asyncio.create_task(
                self._run_snapshot_worker(), name="session-snapshot-worker",
            )

    async def stop(self) -> None:
        for task_attr in ("_evict_task", "_snapshot_task"):
            task = getattr(self, task_attr)
            if task is not None:
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                setattr(self, task_attr, None)
        await self._flusher.stop()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def flusher(self) -> DiskFlusher:
        return self._flusher

    @property
    def allocator(self) -> SeqAllocator:
        return self._allocator

    @property
    def index(self) -> SessionIndex | None:
        return self._index

    async def _maybe_index_upsert(self, state: SessionState) -> None:
        """Push the live summary to the cross-session index. No-op
        when no index is wired. Errors are logged but never raised
        so an index hiccup never breaks the agent loop.

        Skips orphan sessions (no user_id). The index schema has
        ``user_id TEXT NOT NULL`` and the lookup is exact-match, so
        rows with an empty user_id would land but never surface in
        any "list sessions for user X" query -- they'd just clog the
        index with unreachable rows. Better to leave them out and
        rely on the meta.json on disk for forensic recovery.
        """
        if self._index is None:
            return
        if not getattr(state, "user_id", None):
            return  # orphan -- don't pollute the index
        try:
            summary = SessionSummary.from_state_summary(state.summary())
            await self._index.upsert(summary)
        except Exception as exc:
            logger.warning(
                "session_index_upsert_failed sid=%s err=%s",
                state.session_id, exc,
            )

    def _session_dir(self, sid: str) -> Path:
        h = hashlib.sha256(sid.encode("utf-8")).hexdigest()
        return self._root / h[:2] / h[2:4] / sid

    def _chat_meta_for_flush(self, sid: str) -> dict:
        """Phase 1+2: snapshot of the live chat-level state, merged
        into meta.json on every flush batch by the DiskFlusher.

        Includes ``app_id`` + ``user_id`` so cross-session lookups
        (``list_for_app``, ``get_any_owner``, ``delete_for_app``) can
        scan meta.json files without needing to load events.jsonl or
        the SQLite index. Returns ``{}`` when the session is not
        loaded (already evicted) -- in that case meta.json keeps
        whatever was last written.
        """
        st = self._sessions.get(sid)
        if st is None:
            return {}
        return {
            # Identity (Phase 2): needed by list_for_app / get_any_owner FS
            # fallback. Without these the meta.json is opaque about who
            # owns the session and the lookups fail silently.
            "app_id": st.app_id,
            "user_id": st.user_id,
            # Phase 1 chat-level fields.
            "title": st.title,
            "turn_count": st.turn_count,
            "workspace": st.workspace,
            "workdir": st.workdir,
            "interrupted": st.interrupted,
            "interrupted_at": st.interrupted_at,
            "cost_total": st.cost_total,
            "tokens_in": st.tokens_in,
            "tokens_out": st.tokens_out,
        }

    def _seed_seq_from_disk(self, scope_key: str) -> int:
        """SeqAllocator seed_loader: read meta.json's last_seq for the
        scope's session. User-scope keys (no session_id) seed at 0
        because user-scope events are not persisted per-session.

        O(1) when meta.json exists. O(file-size) fallback to JSONL
        tail scan when meta is missing (cold restart edge case).
        """
        if not scope_key.startswith("session::"):
            return 0
        sid = scope_key[len("session::"):]
        session_dir = self._session_dir(sid)
        meta = MetaIO.read(session_dir)
        if meta is not None and "last_seq" in meta:
            return int(meta["last_seq"])
        return MetaIO.last_seq_from_jsonl_tail(session_dir)

    async def open(
        self,
        sid: str,
        *,
        app_id: str,
        user_id: str,
        create_if_missing: bool = True,
        pin: bool = True,
    ) -> SessionState:
        """Open (or create) a session. If it exists on disk, reload
        its state from events.jsonl. Pin=True (default) means it is
        an actively-being-written session and will not be evicted."""
        async with self._sessions_lock:
            state = self._sessions.get(sid)
            was_loaded = state is None
            if state is None:
                state = await self._load_or_create(
                    sid=sid, app_id=app_id, user_id=user_id,
                    create_if_missing=create_if_missing,
                )
                self._sessions[sid] = state
                self._current_bytes += state.bytes_estimate
            state.touch()
            self._sessions.move_to_end(sid)
            if pin:
                state.pinned = True
        # Index upsert OUTSIDE the sessions_lock to avoid holding it
        # during the SQLite I/O. Only fires on first open per process
        # (was_loaded) and on a real user-bound session -- ``_maybe_
        # index_upsert`` itself skips orphans. Without this call, a
        # freshly-opened session is on disk + in RAM but invisible to
        # ``list_for_user`` until close_session / compact_session
        # fires; the frontend's "list my sessions" returns 0 for
        # active sessions.
        if was_loaded:
            await self._maybe_index_upsert(state)
            # Resume-time seed sync. When a session is reloaded from
            # disk with ``last_seq=N``, the wire-side EventBuffer (a
            # separate allocator process-wide) is still at 0 for this
            # session because its legacy seed_loader queries the
            # ``history_log`` table -- which is no longer written
            # post Phase-4c, so the query returns 0 even though the
            # SessionStore has N events on disk. Without this push,
            # the wire allocates seq=1, 2, ... and every emit is
            # rejected by ``append_event`` as a regression below
            # ``state.last_seq``. Pushing the on-disk high-water mark
            # into the wire allocator forces its next ``next_seq`` to
            # return N+1, matching what's on disk. Reuses the same
            # hook used for internal allocations -- the semantic is
            # "the store has seen seq=K for this session, sync your
            # state".
            if (
                state.last_seq > 0
                and self._on_internal_seq_alloc is not None
            ):
                try:
                    self._on_internal_seq_alloc(sid, int(state.last_seq))
                except Exception as exc:
                    logger.debug(
                        "on_internal_seq_alloc resume-seed hook failed "
                        "sid=%s last_seq=%s: %s",
                        sid, state.last_seq, exc,
                    )
        return state

    async def _load_or_create(
        self, *, sid: str, app_id: str, user_id: str,
        create_if_missing: bool,
    ) -> SessionState:
        session_dir = self._session_dir(sid)
        if not session_dir.exists():
            if not create_if_missing:
                raise KeyError(f"session_not_found: {sid}")
            return SessionState(
                session_id=sid, app_id=app_id, user_id=user_id,
            )
        meta = MetaIO.read(session_dir) or {}
        resolved_app = str(meta.get("app_id") or app_id)
        resolved_user = str(meta.get("user_id") or user_id)
        events_on_disk = await asyncio.to_thread(
            self._read_events_jsonl, session_dir,
        )
        if not resolved_app and events_on_disk:
            resolved_app = events_on_disk[0].app_id or ""
        if not resolved_user and events_on_disk:
            resolved_user = events_on_disk[0].user_id or ""
        state = SessionState(
            session_id=sid, app_id=resolved_app, user_id=resolved_user,
        )
        parent_link = self._read_parent_link(session_dir)
        if parent_link is not None:
            state.parent_link = parent_link

        comp = read_compaction(session_dir)
        if comp is None:
            for ev in events_on_disk:
                state.events.append(ev)
                state.last_seq = ev.seq
                apply_projection(state, ev)
                state.bytes_estimate += ev.size_bytes()
            if events_on_disk:
                state.first_seq = events_on_disk[0].seq
                state.last_flushed_seq = events_on_disk[-1].seq
        else:
            self._restore_with_compaction(
                state=state,
                comp=comp,
                events_on_disk=events_on_disk,
                session_dir=session_dir,
            )

        if "started_at" in meta:
            state.started_at = str(meta["started_at"])
        if meta.get("ended_at"):
            state.ended_at = str(meta["ended_at"])
            state.closed = True
        # Phase 1: ConversationSession-absorbed fields. Tolerate
        # legacy meta.json (pre-Phase-1) where they may be absent.
        state.title = str(meta.get("title", "") or "")
        state.turn_count = int(meta.get("turn_count", 0) or 0)
        state.workspace = str(meta.get("workspace", "") or "")
        state.workdir = str(meta.get("workdir", "") or "")
        state.interrupted = bool(meta.get("interrupted", False))
        state.interrupted_at = (
            str(meta["interrupted_at"]) if meta.get("interrupted_at") else None
        )
        return state

    def _restore_with_compaction(
        self,
        *,
        state: SessionState,
        comp: Compaction,
        events_on_disk: list[Event],
        session_dir: Path,
    ) -> None:
        """Apply a saved compaction on a freshly-loaded SessionState.

        The snapshot.json captures the FULL pre-compaction state at
        time T (``snap.last_seq``). Events post-cutoff but <= T are
        already reflected in the snapshot's projections; only events
        with seq > T are deltas that need replaying.

        Strategy:
          1. Restore small projections from snapshot.json (todos,
             memory, workspace, tool_calls, children, blobs, costs).
          2. Restore messages from snapshot, filtered to seq > cutoff.
          3. Append events post-cutoff to ``state.events`` for the
             journal. Apply projection ONLY to events post-snapshot
             so we don't double-count what's already in the snapshot.
          4. Inject the summary as a system message at messages[0].
        """
        snap = read_snapshot(session_dir) or {}
        snap_last_seq = int(snap.get("last_seq", 0))

        state.todos = [
            Todo.from_dict(t) for t in snap.get("todos", [])
        ]
        state.memory_facts = dict(snap.get("memory_facts", {}))
        state.workspace_files = {
            p: FileState.from_dict(f)
            for p, f in (snap.get("workspace_files") or {}).items()
        }
        state.tool_calls = {
            k: ToolCall.from_dict(v)
            for k, v in (snap.get("tool_calls") or {}).items()
        }
        state.tool_results = {
            k: ToolResult.from_dict(v)
            for k, v in (snap.get("tool_results") or {}).items()
        }
        state.children = [
            ChildAgentRef.from_dict(c) for c in snap.get("children", [])
        ]
        state.blobs = {
            h: BlobRef.from_dict(b)
            for h, b in (snap.get("blobs") or {}).items()
        }
        state.cost_total = float(snap.get("cost_total", 0.0))
        state.tokens_in = int(snap.get("tokens_in", 0))
        state.tokens_out = int(snap.get("tokens_out", 0))
        # Phase 1: ConversationSession-absorbed fields. Snapshot is
        # authoritative when present (the close-time write captured
        # the most recent values); meta.json fills in if a snapshot
        # is missing.
        state.title = str(snap.get("title", "") or "")
        state.turn_count = int(snap.get("turn_count", 0) or 0)
        state.workspace = str(snap.get("workspace", "") or "")
        state.workdir = str(snap.get("workdir", "") or "")
        state.interrupted = bool(snap.get("interrupted", False))
        state.interrupted_at = (
            str(snap["interrupted_at"]) if snap.get("interrupted_at") else None
        )

        state.messages = [
            Message.from_dict(m)
            for m in snap.get("messages", [])
            if int(m.get("seq", 0)) > comp.cutoff_seq
        ]

        for ev in events_on_disk:
            if ev.seq <= comp.cutoff_seq:
                continue
            state.events.append(ev)
            state.last_seq = ev.seq
            state.bytes_estimate += ev.size_bytes()
            if ev.seq > snap_last_seq:
                apply_projection(state, ev)

        if events_on_disk:
            state.last_flushed_seq = events_on_disk[-1].seq
        state.first_seq = (
            state.events[0].seq if state.events else comp.cutoff_seq + 1
        )

        summary_msg = Message(
            role="system",
            content=f"[Previous context summary]\n{comp.summary}",
            seq=comp.cutoff_seq,
            ts=comp.created_at,
        )
        state.messages.insert(0, summary_msg)
        state.applied_compaction = comp

    @staticmethod
    def _read_parent_link(session_dir: Path) -> ParentLink | None:
        path = session_dir / "parent_link.json"
        if not path.exists():
            return None
        try:
            return ParentLink.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, OSError, KeyError, TypeError) as exc:
            logger.warning(
                "parent_link_read_corrupt path=%s err=%s", path, exc,
            )
            return None

    @staticmethod
    def _write_parent_link(session_dir: Path, link: ParentLink) -> None:
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / "parent_link.json"
        path.write_text(
            json.dumps(link.to_dict(), default=str, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def _read_events_jsonl(session_dir: Path) -> list[Event]:
        path = session_dir / "events.jsonl"
        if not path.exists():
            return []
        events: list[Event] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(Event.from_dict(json.loads(line)))
                except (json.JSONDecodeError, TypeError) as exc:
                    logger.warning(
                        "events_jsonl_skip_bad_line path=%s err=%s",
                        path, exc,
                    )
        return events

    def state(self, sid: str) -> SessionState | None:
        """Sync, no-await. Returns None if session not loaded.
        O(1) hot-path read for the agent loop's tight loop."""
        return self._sessions.get(sid)

    async def append_event(self, sid: str, event: Event) -> int:
        """The single point of entry for ANY event.

        Returns the assigned ``seq``. Caller must use this seq when
        broadcasting to clients (don't generate a parallel seq).

        SEQ CONTRACT (locked, never break this):
        If ``event.seq`` is > 0 on entry, the caller pre-allocated it
        via the canonical wire-level allocator (EventBuffer) and the
        client has already observed that exact number. We MUST keep it
        as-is so the persisted seq matches the wire seq -- otherwise a
        replay would surface the same event under a different seq and
        the frontend's strict-seq dedup would treat it as a ghost.
        The local allocator is bumped forward so any subsequent
        seq-less append() doesn't collide.
        """
        t0 = time.perf_counter()
        state = self._sessions.get(sid)
        if state is None:
            raise KeyError(
                f"session_not_open: {sid} -- call open() before append_event()"
            )

        if event.seq and event.seq > 0:
            # Caller-supplied seq: respect it, advance local high-water
            # mark forward so future seq-less appends don't collide.
            if event.seq > self._allocator.latest(session_id=sid):
                self._allocator.force_seed(
                    f"session::{sid}", int(event.seq),
                )
        else:
            event.seq = self._allocator.next(
                user_id=state.user_id, session_id=sid,
            )
            # Back-propagate to the wire allocator so EventBuffer
            # knows this seq is burned and never returns it again.
            # Without this hook, internal callers (compact_done,
            # agent_spawn) would allocate K here while EventBuffer
            # stays at K-1, and the next wire emit would also get K
            # -> duplicate seq on the client timeline.
            if self._on_internal_seq_alloc is not None:
                try:
                    self._on_internal_seq_alloc(sid, int(event.seq))
                except Exception as exc:
                    logger.debug(
                        "on_internal_seq_alloc hook error sid=%s seq=%s: %s",
                        sid, event.seq, exc,
                    )
        if not event.session_id:
            event.session_id = sid
        if not event.app_id:
            event.app_id = state.app_id
        if event.user_id is None:
            event.user_id = state.user_id

        state.events.append(event)
        if state.first_seq == 0:
            state.first_seq = event.seq
        state.last_seq = event.seq

        apply_projection(state, event)

        size = event.size_bytes()
        state.bytes_estimate += size
        self._current_bytes += size
        state.touch()
        # Phase 6: only ``append_event`` advances ``last_event_at`` --
        # the bg snapshot worker uses this to detect idle sessions
        # without read traffic resetting the timer.
        state.last_event_at = time.monotonic()
        self._sessions.move_to_end(sid)

        self._flusher.enqueue(sid, event)

        # Throttled index refresh so the cross-session list view
        # surfaces fresh ``last_seq`` / ``last_event_at`` while the
        # session is active, not only at close_session / compact_session
        # time. Fires every 20 events; the SQLite upsert is ~0.3 ms so
        # the cost is negligible at chat-throughput rates. Orphans (no
        # user_id) are skipped inside ``_maybe_index_upsert``.
        if self._index is not None and (state.last_seq % 20 == 0):
            try:
                asyncio.ensure_future(self._maybe_index_upsert(state))
            except RuntimeError:
                # No running loop (rare: synchronous test harness).
                pass

        if self._current_bytes > self._max_bytes \
                or len(self._sessions) > self._max_sessions:
            self._evict_signal.set()

        self._append_durations_ms.append(
            (time.perf_counter() - t0) * 1000.0,
        )
        return event.seq

    async def close_session(self, sid: str) -> None:
        """End-of-session: flush queue, write snapshot.json, mark
        closed in meta.json, upsert summary into the cross-session
        index (if attached), unpin so the session becomes evictable."""
        state = self._sessions.get(sid)
        if state is None:
            return
        await self._flusher.flush()
        state.closed = True
        if state.ended_at is None:
            state.ended_at = utc_iso()
        snap_seq = state.last_seq
        await asyncio.to_thread(self._persist_close, sid, state)
        state.last_snapshot_seq = snap_seq
        await self._maybe_index_upsert(state)
        state.pinned = False

    def _persist_close(self, sid: str, state: SessionState) -> None:
        session_dir = self._session_dir(sid)
        MetaIO.update(
            session_dir,
            session_id=sid,
            app_id=state.app_id,
            user_id=state.user_id,
            ended_at=state.ended_at,
            closed=True,
            event_count=len(state.events),
            first_seq=state.first_seq,
            last_seq=state.last_seq,
            cost_total=state.cost_total,
            tokens_in=state.tokens_in,
            tokens_out=state.tokens_out,
            # Phase 1: ConversationSession-absorbed fields.
            title=state.title,
            turn_count=state.turn_count,
            workspace=state.workspace,
            workdir=state.workdir,
            interrupted=state.interrupted,
            interrupted_at=state.interrupted_at,
        )
        write_snapshot(session_dir, build_snapshot(state))

    async def save_snapshot(self, sid: str) -> bool:
        """Force-write a snapshot of the current state without closing
        the session. Useful for periodic checkpoints on long-running
        chats so a crash mid-session still has a recent UI snapshot."""
        state = self._sessions.get(sid)
        if state is None:
            return False
        await self._flusher.flush()
        snap_seq = state.last_seq
        snap = build_snapshot(state)
        await asyncio.to_thread(
            write_snapshot, self._session_dir(sid), snap,
        )
        state.last_snapshot_seq = snap_seq
        return True

    async def read_snapshot(self, sid: str) -> dict | None:
        """Fast reopen path. ``5 ms`` cold, ``<100 µs`` warm.

        Reads ``snapshot.json`` directly from disk. The frontend
        renders this without needing to replay every event. Returns
        ``None`` when no snapshot exists (session never closed, or a
        crash happened before snapshot.json was written) -- caller
        should fall back to ``stream_events`` then.
        """
        return await asyncio.to_thread(
            read_snapshot, self._session_dir(sid),
        )

    async def spawn_child(
        self,
        *,
        parent_sid: str,
        child_sid: str,
        kind: str,
        app_id: str | None = None,
        user_id: str | None = None,
    ) -> SessionState:
        """Spawn a sub-agent under ``parent_sid``.

        Effects:
          1. Open (create) the child session, store its parent_link.
          2. Persist parent_link.json next to the child's events.jsonl.
          3. Emit an ``agent_spawn`` event on the PARENT, which feeds
             the projection into ``parent.children`` automatically.

        ``app_id`` and ``user_id`` default to the parent's. The child
        gets its own seq counter (per-session scope), so its events
        are independent of the parent's seq sequence.
        """
        parent = self._sessions.get(parent_sid)
        if parent is None:
            raise KeyError(
                f"parent_session_not_open: {parent_sid} -- "
                "open the parent before spawning a child"
            )
        resolved_app = app_id if app_id is not None else parent.app_id
        resolved_user = user_id if user_id is not None else parent.user_id
        link = ParentLink(
            parent_session_id=parent_sid,
            parent_seq_at_spawn=parent.last_seq,
            child_kind=kind,
        )
        child = await self.open(
            child_sid, app_id=resolved_app, user_id=resolved_user, pin=True,
        )
        child.parent_link = link
        await asyncio.to_thread(
            self._write_parent_link, self._session_dir(child_sid), link,
        )
        await self.append_event(parent_sid, Event(
            type="agent_spawn",
            payload={
                "run_id": child_sid,
                "specialist": kind,
                "kind": kind,
                "child_session_id": child_sid,
            },
        ))
        return child

    def list_children(self, parent_sid: str) -> list:
        """Return the in-memory ChildAgentRef list maintained by
        the agent_spawn / agent_result projection. Returns ``[]`` if
        the parent isn't loaded -- caller should ``open`` it first."""
        state = self._sessions.get(parent_sid)
        if state is None:
            return []
        return list(state.children)

    async def compact_session(
        self,
        sid: str,
        *,
        cutoff_seq: int,
        summary: str,
        strategy: str = "summary_plus_keys",
        key_events: list[Event] | None = None,
        tokens_estimate: int,
        model: str,
    ) -> Compaction:
        """Compact the session: drop RAM events/messages with
        seq <= cutoff_seq, persist compaction.json + snapshot.json,
        emit a ``compact_done`` event carrying the new context.

        events.jsonl is NEVER touched. Frontend ``stream_full_history``
        keeps seeing the full chronology.

        ``tokens_estimate`` MUST be a real value computed via
        ``token_counter.count_message_tokens(model, [...])`` -- no
        len/4 heuristic. The caller is responsible for providing it.

        The compact_done event's payload carries the FULL new context
        state (messages, todos, memory, workspace, tools, children,
        blobs, costs) and is stamped with the next monotonic seq from
        the SeqAllocator. Frontend listeners use it to refresh their
        rendered context without re-fetching.

        Returns the persisted ``Compaction`` record.
        """
        state = self._sessions.get(sid)
        if state is None:
            raise KeyError(
                f"session_not_open: {sid} -- open() before compact_session()"
            )
        if cutoff_seq < 0 or cutoff_seq > state.last_seq:
            raise ValueError(
                f"cutoff_seq={cutoff_seq} out of range "
                f"[0, {state.last_seq}] for session {sid}"
            )

        session_dir = self._session_dir(sid)

        full_snapshot = build_snapshot(state)
        await asyncio.to_thread(write_snapshot, session_dir, full_snapshot)

        comp = Compaction(
            cutoff_seq=cutoff_seq,
            summary=summary,
            strategy=strategy,
            key_events=[e.to_dict() for e in (key_events or [])],
            created_at=utc_iso(),
            tokens_estimate=int(tokens_estimate),
            model=model,
        )
        await asyncio.to_thread(write_compaction, session_dir, comp)

        bytes_dropped = sum(
            ev.size_bytes() for ev in state.events if ev.seq <= cutoff_seq
        )
        state.events = [ev for ev in state.events if ev.seq > cutoff_seq]
        state.messages = [m for m in state.messages if m.seq > cutoff_seq]
        state.bytes_estimate = max(0, state.bytes_estimate - bytes_dropped)
        self._current_bytes = max(0, self._current_bytes - bytes_dropped)

        summary_msg = Message(
            role="system",
            content=f"[Previous context summary]\n{summary}",
            seq=cutoff_seq,
            ts=comp.created_at,
        )
        state.messages.insert(0, summary_msg)
        state.applied_compaction = comp
        state.first_seq = (
            state.events[0].seq if state.events else cutoff_seq + 1
        )

        compact_done = Event(
            type="compact_done",
            kind="event",
            payload={
                "cutoff_seq": cutoff_seq,
                "summary": summary,
                "strategy": strategy,
                "tokens_estimate": int(tokens_estimate),
                "model": model,
                "key_event_count": len(key_events or []),
                "compaction_at_ts": comp.created_at,
                "context_after": {
                    "messages": [m.to_dict() for m in state.messages],
                    "todos": [t.to_dict() for t in state.todos],
                    "memory_facts": dict(state.memory_facts),
                    "workspace_files": {
                        p: f.to_dict()
                        for p, f in state.workspace_files.items()
                    },
                    "tool_calls": {
                        k: v.to_dict() for k, v in state.tool_calls.items()
                    },
                    "tool_results": {
                        k: v.to_dict() for k, v in state.tool_results.items()
                    },
                    "children": [c.to_dict() for c in state.children],
                    "blobs": {
                        h: b.to_dict() for h, b in state.blobs.items()
                    },
                    "cost_total": state.cost_total,
                    "tokens_in": state.tokens_in,
                    "tokens_out": state.tokens_out,
                    "first_seq": state.first_seq,
                },
            },
        )
        await self.append_event(sid, compact_done)
        await self._maybe_index_upsert(state)
        return comp

    async def stream_full_history(
        self, sid: str, *, since: int = 0,
    ) -> AsyncIterator[Event]:
        """Stream events.jsonl directly from disk, IGNORING any
        compaction. Used by the frontend / UI replay so the user
        sees the entire chronology even when the agent's RAM has
        been compacted to a smaller window.
        """
        session_dir = self._session_dir(sid)
        events = await asyncio.to_thread(
            self._read_events_jsonl, session_dir,
        )
        for ev in events:
            if ev.seq > since:
                yield ev

    async def stream_events(
        self, sid: str, *, since: int = 0,
    ) -> AsyncIterator[Event]:
        """Yield events with ``seq > since`` in monotonic order.

        Hot path: from the in-memory journal.
        Cold path (session evicted): re-load from disk.
        Returns nothing if the session does not exist anywhere.
        """
        state = self._sessions.get(sid)
        if state is None:
            try:
                await self.open(
                    sid, app_id="", user_id="",
                    create_if_missing=False, pin=False,
                )
            except KeyError:
                return
            state = self._sessions.get(sid)
        if state is None:
            return
        for ev in state.events:
            if ev.seq > since:
                yield ev

    def list_in_memory_sessions(self) -> list[str]:
        return list(self._sessions.keys())

    async def _maybe_evict(self) -> None:
        """LRU evict non-pinned sessions until under cap. Flush
        queue first so no in-flight events are lost."""
        async with self._sessions_lock:
            if self._current_bytes <= self._max_bytes \
                    and len(self._sessions) <= self._max_sessions:
                return
            await self._flusher.flush()
            for sid in list(self._sessions.keys()):
                if self._current_bytes <= self._max_bytes \
                        and len(self._sessions) <= self._max_sessions:
                    break
                state = self._sessions[sid]
                if state.pinned:
                    continue
                self._current_bytes -= state.bytes_estimate
                del self._sessions[sid]
                # Phase 2: drop the per-session lock too. A future open
                # of the same sid will lazily allocate a fresh one.
                self._per_session_locks.pop(sid, None)

    async def evict(self, sid: str) -> bool:
        """Manually evict a non-pinned session. Returns True if
        evicted, False if session is pinned or not loaded."""
        async with self._sessions_lock:
            state = self._sessions.get(sid)
            if state is None:
                return False
            if state.pinned:
                return False
            await self._flusher.flush()
            self._current_bytes -= state.bytes_estimate
            del self._sessions[sid]
            self._per_session_locks.pop(sid, None)
            return True

    async def _run_eviction_worker(self) -> None:
        """Single long-running worker: drain ``_evict_signal``, run
        ``_maybe_evict``, repeat. Replaces per-append ``create_task``
        which allocated one task per overflowing event.

        If a pass freed nothing (everything pinned), back off briefly
        so we don't busy-spin while the budget stays violated.
        """
        BUSY_BACKOFF_S = 1.0
        while True:
            await self._evict_signal.wait()
            self._evict_signal.clear()
            try:
                before = len(self._sessions)
                await self._maybe_evict()
                after = len(self._sessions)
                if before == after and (
                    self._current_bytes > self._max_bytes
                    or len(self._sessions) > self._max_sessions
                ):
                    await asyncio.sleep(BUSY_BACKOFF_S)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "eviction_worker_iter_failed err=%s", exc,
                )
                await asyncio.sleep(0.1)

    async def _run_snapshot_worker(self) -> None:
        """Periodic background snapshot writer.

        Walks loaded sessions every SCAN_INTERVAL_S; for each one with
        >= SNAPSHOT_DELTA new events since its last snapshot AND idle
        for >= IDLE_THRESHOLD_S, builds + writes ``snapshot.json``.
        That cuts cold-start reload time after a daemon crash from
        O(events) to O(snapshot) for live sessions, without touching
        the hot append path.
        """
        SNAPSHOT_DELTA = 50
        IDLE_THRESHOLD_S = 5.0
        SCAN_INTERVAL_S = 10.0
        while True:
            try:
                await asyncio.sleep(SCAN_INTERVAL_S)
                ripe = self._find_ripe_for_snapshot(
                    delta=SNAPSHOT_DELTA, idle_s=IDLE_THRESHOLD_S,
                )
                for sid in ripe:
                    try:
                        await self._snapshot_one(sid)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "snapshot_one_failed sid=%s err=%s", sid, exc,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "snapshot_worker_iter_failed err=%s", exc,
                )

    def _find_ripe_for_snapshot(
        self, *, delta: int, idle_s: float,
    ) -> list[str]:
        now = time.monotonic()
        ripe: list[str] = []
        for sid, st in self._sessions.items():
            if st.closed:
                continue
            if (st.last_seq - st.last_snapshot_seq) < delta:
                continue
            # Phase 6: gate on ``last_event_at`` (advanced only by
            # append_event), NOT ``last_accessed_at`` (advanced by every
            # read too). Otherwise reads from /history etc. permanently
            # reset the idle clock under any load.
            if (now - st.last_event_at) < idle_s:
                continue
            ripe.append(sid)
        return ripe

    async def _snapshot_one(self, sid: str) -> None:
        """Build a snapshot from live state (sync, atomic on the loop)
        then offload the write to a thread.

        Snap is captured at the in-memory ``state.last_seq`` -- the
        disk flusher drains independently. ``snap.last_seq`` may
        briefly exceed ``meta.last_seq`` on disk; the reload path
        (``_load_or_create``) tolerates that and replays events past
        the snapshot cutoff.
        """
        state = self._sessions.get(sid)
        if state is None or state.closed:
            return
        snap_seq = state.last_seq
        snap = build_snapshot(state)
        session_dir = self._session_dir(sid)
        await asyncio.to_thread(write_snapshot, session_dir, snap)
        state.last_snapshot_seq = snap_seq

    # ── Phase 2 primitives ───────────────────────────────────────────

    def session_lock(self, sid: str) -> asyncio.Lock:
        """Return the asyncio.Lock for ``sid``, allocating it on first
        access. Sync (no event loop ops) so the legacy adapter can
        forward calls without bridging async->sync.

        The lock object is identical across calls for the same sid.
        Callers do ``async with store.session_lock(sid):`` to
        serialise critical sections per session.

        Race-safe via ``dict.setdefault`` -- if two coroutines
        concurrently allocate, one wins atomically (CPython GIL) and
        the loser's freshly-created Lock is GC'd. Both callers see
        the same instance returned."""
        return self._per_session_locks.setdefault(sid, asyncio.Lock())

    async def delete(
        self, sid: str, *, force: bool = False,
    ) -> bool:
        """Delete a session: drop in-memory state, flush pending events,
        remove the session dir from disk, drop the index entry, and
        release the per-session lock.

        ``force=False`` (default) refuses to delete a pinned session
        (still actively chatting). ``force=True`` evicts the pin and
        deletes anyway -- use only on hard cleanup paths.

        Returns True if the session existed and was removed, False
        otherwise. Idempotent: deleting a non-existent session is a
        no-op that returns False.
        """
        async with self._sessions_lock:
            state = self._sessions.get(sid)
            if state is not None:
                if state.pinned and not force:
                    return False
                # Drop any pending events from the flusher's queue for
                # this sid first; they'd recreate the dir post-delete
                # otherwise.
                await self._flusher.flush()
                self._current_bytes -= state.bytes_estimate
                del self._sessions[sid]
            self._per_session_locks.pop(sid, None)

        # Disk + index cleanup runs OUTSIDE the meta lock so a slow
        # IO doesn't block other open()/delete()s.
        sdir = self._session_dir(sid)
        existed_on_disk = sdir.exists()
        if existed_on_disk:
            await asyncio.to_thread(self._purge_session_dir, sdir)

        if self._index is not None:
            try:
                await self._index.delete(sid)
            except Exception as exc:
                logger.warning(
                    "session_index_delete_failed sid=%s err=%s",
                    sid, exc,
                )

        return existed_on_disk or state is not None

    @staticmethod
    def _purge_session_dir(sdir: Path) -> None:
        """Remove a session dir entirely. Tolerates missing files,
        in-progress writes from another thread (unlikely once flush()
        completed). Logs and re-raises hard errors so the caller can
        surface them."""
        import shutil
        if sdir.exists():
            shutil.rmtree(sdir, ignore_errors=False)

    async def delete_for_app(self, app_id: str) -> int:
        """Delete every session owned by ``app_id``. Returns the count
        of sessions removed. Used by the legacy SessionStore API
        contract (``delete_for_app`` on app uninstall)."""
        sids = await self.list_session_ids_for_app(app_id)
        deleted = 0
        for sid in sids:
            try:
                if await self.delete(sid, force=True):
                    deleted += 1
            except Exception as exc:
                logger.warning(
                    "delete_for_app_one_failed app=%s sid=%s err=%s",
                    app_id, sid, exc,
                )
        return deleted

    async def list_session_ids_for_app(self, app_id: str) -> list[str]:
        """Return all session_ids belonging to ``app_id``. Combines the
        SQLite index (O(log n) for closed/compacted sessions) AND a
        filesystem walk (O(n) for active-but-unclosed sessions whose
        index entry hasn't been upserted yet). Returns the union
        deduplicated.

        The dual-source design matters because the index is upserted
        only on ``close_session`` / ``compact_session`` -- a session
        that's been chatting all day but never closed wouldn't show
        up in the index. The FS walk picks it up via the meta.json
        the flusher refreshes on every batch.
        """
        seen: set[str] = set()
        if self._index is not None:
            try:
                summaries = await self._index.list_for_app(app_id)
                for s in summaries:
                    if s.session_id:
                        seen.add(s.session_id)
            except Exception as exc:
                logger.warning(
                    "session_index_list_failed app=%s err=%s -- "
                    "falling back to filesystem walk only", app_id, exc,
                )
        fs_sids = await asyncio.to_thread(
            self._list_session_ids_for_app_fs, app_id,
        )
        for sid in fs_sids:
            seen.add(sid)
        return sorted(seen)

    def _list_session_ids_for_app_fs(self, app_id: str) -> list[str]:
        """Filesystem fallback: walk ``self._root`` reading meta.json
        files. Skips dirs without a parsable meta.json."""
        out: list[str] = []
        if not self._root.exists():
            return out
        for meta_path in self._root.rglob("meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.debug(
                    "list_for_app_fs_skip path=%s err=%s",
                    meta_path, exc,
                )
                continue
            if str(meta.get("app_id", "")) == app_id:
                sid = str(meta.get("session_id") or meta_path.parent.name)
                if sid:
                    out.append(sid)
        return out

    async def list_for_app(self, app_id: str):
        """Return a list of ``SessionSummary`` for every session of
        ``app_id``. Uses the SQLite index when present (O(log n)),
        otherwise loads each session's metadata from disk."""
        from digitorn.core.runtime.session_store.session_index import (
            SessionSummary,
        )
        if self._index is not None:
            try:
                return await self._index.list_for_app(app_id)
            except Exception as exc:
                logger.warning(
                    "session_index_list_for_app_failed app=%s err=%s",
                    app_id, exc,
                )
        # Filesystem fallback.
        sids = await self.list_session_ids_for_app(app_id)
        out = []
        for sid in sids:
            sdir = self._session_dir(sid)
            try:
                meta = await asyncio.to_thread(
                    lambda p=sdir: json.loads(
                        (p / "meta.json").read_text(encoding="utf-8"),
                    ),
                )
            except Exception:
                continue
            out.append(SessionSummary(
                session_id=sid,
                app_id=str(meta.get("app_id", "")),
                user_id=str(meta.get("user_id", "")),
                started_at=str(meta.get("started_at", "")),
                ended_at=meta.get("ended_at"),
                closed=bool(meta.get("closed", False)),
                last_seq=int(meta.get("last_seq", 0) or 0),
                event_count=int(meta.get("event_count", 0) or 0),
                cost_total=float(meta.get("cost_total", 0.0) or 0.0),
                tokens_in=int(meta.get("tokens_in", 0) or 0),
                tokens_out=int(meta.get("tokens_out", 0) or 0),
                title=meta.get("title"),
            ))
        return out

    async def get_any_owner(self, app_id: str, sid: str) -> str | None:
        """Return the user_id that owns ``(app_id, sid)``, irrespective
        of the caller's identity. Used by cross-user lookup paths in
        the legacy API surface (e.g. ``apps_v2/sessions.py`` resolve a
        session that was created by another user but is being read
        through an admin endpoint).

        ``None`` when the session doesn't exist or has no recorded
        owner. Reads meta.json directly -- O(1) on hot path. The
        index isn't required.
        """
        sdir = self._session_dir(sid)
        path = sdir / "meta.json"
        if not path.exists():
            return None
        try:
            meta = await asyncio.to_thread(
                lambda: json.loads(path.read_text(encoding="utf-8")),
            )
        except Exception as exc:
            logger.debug("get_any_owner_meta_read_failed sid=%s err=%s", sid, exc)
            return None
        if str(meta.get("app_id", "")) != app_id:
            return None
        owner = str(meta.get("user_id", "") or "")
        return owner or None

    async def recover_orphans(self) -> int:
        """Boot-time recovery: walk the sessions root, find sessions
        that were active but never closed cleanly, mark them
        ``interrupted=true`` so the next reopen surfaces the
        "smart resume" UI.

        FAST by design: reads ONLY each session's meta.json (small,
        already memory-mapped on Windows). Does NOT load the full
        events.jsonl. Sessions that need resume logic load lazily on
        first access -- at boot we just stamp the marker.

        Returns the count of sessions marked interrupted.
        """
        marked = 0
        if not self._root.exists():
            return marked
        # ``to_thread`` because rglob + read_text are sync IO. Boot
        # path must NEVER stall the loop -- with 10k sessions on disk
        # this would otherwise freeze the daemon for tens of seconds.
        candidates = await asyncio.to_thread(self._collect_orphan_candidates)
        for sdir, meta in candidates:
            if meta.get("interrupted"):
                continue  # already marked
            try:
                await asyncio.to_thread(
                    self._mark_interrupted_sync, sdir, meta,
                )
                marked += 1
            except Exception as exc:
                logger.warning(
                    "recover_orphan_mark_failed sdir=%s err=%s",
                    sdir, exc,
                )
        return marked

    def _collect_orphan_candidates(self) -> list[tuple[Path, dict]]:
        """Sync helper for recover_orphans: walk the sessions root,
        read every meta.json, return (dir, meta) for sessions that
        look unclosed. Cheap enough at boot (one read per session)."""
        out: list[tuple[Path, dict]] = []
        for meta_path in self._root.rglob("meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if meta.get("closed"):
                continue
            if not meta.get("session_id"):
                continue
            out.append((meta_path.parent, meta))
        return out

    @staticmethod
    def _mark_interrupted_sync(sdir: Path, meta: dict) -> None:
        """Sync helper: mark a session interrupted in its meta.json."""
        meta = dict(meta)
        meta["interrupted"] = True
        if not meta.get("interrupted_at"):
            meta["interrupted_at"] = utc_iso()
        MetaIO.write(sdir, meta)

    def stats(self) -> dict:
        return {
            "sessions_in_memory": len(self._sessions),
            "current_bytes": self._current_bytes,
            "max_bytes": self._max_bytes,
            "max_sessions": self._max_sessions,
            "per_session_locks": len(self._per_session_locks),
            "flusher_written": self._flusher.written,
            "flusher_dropped": self._flusher.dropped,
            "flusher_batches": self._flusher.batch_count,
            "flusher_num_shards": self._flusher.num_shards,
            "flusher_durability_mode": self._flusher.durability_mode,
            **self._append_latency_stats(),
        }

    def _append_latency_stats(self) -> dict:
        """p50/p95/p99 over the last ~1024 ``append_event`` samples.
        Returns zeros when not yet primed."""
        n = len(self._append_durations_ms)
        if n == 0:
            return {
                "append_event_p50_ms": 0.0,
                "append_event_p95_ms": 0.0,
                "append_event_p99_ms": 0.0,
                "append_event_samples": 0,
            }
        sorted_durations = sorted(self._append_durations_ms)
        return {
            "append_event_p50_ms": round(sorted_durations[n // 2], 3),
            "append_event_p95_ms": round(
                sorted_durations[min(n - 1, int(n * 0.95))], 3,
            ),
            "append_event_p99_ms": round(
                sorted_durations[min(n - 1, int(n * 0.99))], 3,
            ),
            "append_event_samples": n,
        }
