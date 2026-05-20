"""InMemorySessionStore: orchestrates seq allocation, projections,"""

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
    """Process-wide in-memory store for active + recently-accessed"""

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

        # per-session asyncio.Lock so chat turns serialise on the same session without blocking other sessions.
        self._per_session_locks: dict[str, asyncio.Lock] = {}

        self._allocator = SeqAllocator(seed_loader=self._seed_seq_from_disk)
        # hook lets the wire-side allocator (EventBuffer) sync to internal allocations so it doesn't burn a duplicate seq.
        self._on_internal_seq_alloc = on_internal_seq_alloc
        self._flusher = DiskFlusher(
            session_dir_resolver=self._session_dir,
            chat_meta_resolver=self._chat_meta_for_flush,
            flush_interval_ms=flush_interval_ms,
            num_shards=num_shards,
            durability_mode=durability_mode,
        )

        # signal + worker pattern replaces a per-append create_task that allocated one task per overflowing event.
        self._evict_signal = asyncio.Event()
        self._evict_task: asyncio.Task | None = None

        # periodic snapshot of ripe-and-idle sessions to reduce cold-start reload time after crash.
        self._snapshot_task: asyncio.Task | None = None

        self._append_durations_ms: deque[float] = deque(maxlen=1024)

        # strong refs prevent GC from collecting fire-and-forget index-upsert tasks before they run.
        self._index_upsert_tasks: set[asyncio.Task[Any]] = set()

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
        st = self._sessions.get(sid)
        if st is None:
            return {}
        return {
            "app_id": st.app_id,
            "user_id": st.user_id,
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
        """Open (or create) a session. If it exists on disk, reload"""
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
        # upsert outside sessions_lock to avoid holding it during SQLite I/O; index_upsert itself skips orphans.
        if was_loaded:
            await self._maybe_index_upsert(state)
            # push the on-disk high-water mark into the wire allocator so its next seq is N+1, not 1.
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
        # ConversationSession-absorbed fields. Tolerate legacy meta.json
        # where they may be absent.
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
        """Apply a saved compaction on a freshly-loaded SessionState."""
        snap = read_snapshot(session_dir) or {}
        snap_last_seq = int(snap.get("last_seq", 0))

        state.todos = [
            Todo.from_dict(t) for t in snap.get("todos", [])
        ]
        state.memory_facts = dict(snap.get("memory_facts", {}))
        # Restore working-memory goal + semantic facts (see
        # snapshot.py:build_snapshot for the persisted shape).
        state.goal = str(snap.get("goal", "") or "")
        state.semantic_facts = list(snap.get("semantic_facts", []) or [])
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
        state.title = str(snap.get("title", "") or "")
        state.turn_count = int(snap.get("turn_count", 0) or 0)
        state.workspace = str(snap.get("workspace", "") or "")
        state.workdir = str(snap.get("workdir", "") or "")
        state.interrupted = bool(snap.get("interrupted", False))
        state.interrupted_at = (
            str(snap["interrupted_at"]) if snap.get("interrupted_at") else None
        )
        _amid = snap.get("active_mode_id")
        state.active_mode_id = (
            str(_amid) if isinstance(_amid, str) and _amid else None
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
        """Sync, no-await"""
        return self._sessions.get(sid)

    async def append_event(self, sid: str, event: Event) -> int:
        """The single point of entry for ANY event."""
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
        state.last_event_at = time.monotonic()
        self._sessions.move_to_end(sid)

        self._flusher.enqueue(sid, event)

        if self._index is not None and (state.last_seq % 20 == 0):
            try:
                _task = asyncio.ensure_future(self._maybe_index_upsert(state))
                self._index_upsert_tasks.add(_task)
                _task.add_done_callback(self._index_upsert_tasks.discard)
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
        """End-of-session: flush queue, write snapshot.json, mark"""
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
            # ConversationSession-absorbed fields.
            title=state.title,
            turn_count=state.turn_count,
            workspace=state.workspace,
            workdir=state.workdir,
            interrupted=state.interrupted,
            interrupted_at=state.interrupted_at,
        )
        write_snapshot(session_dir, build_snapshot(state))

    async def save_snapshot(self, sid: str) -> bool:
        """Force-write a snapshot of the current state without closing"""
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
        """Fast reopen path. `5 ms` cold, `<100 µs` warm."""
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
        """Spawn a sub-agent under `parent_sid`."""
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
        """Return the in-memory ChildAgentRef list maintained by"""
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
        """Compact the session: drop RAM events/messages with"""
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
        """Stream events.jsonl directly from disk, IGNORING any"""
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
        """Yield events with `seq > since` in monotonic order."""
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
        """LRU evict non-pinned sessions until under cap. Flush"""
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
                # Drop the per-session lock too; a future open of the
                # same sid will lazily allocate a fresh one.
                self._per_session_locks.pop(sid, None)

    async def evict(self, sid: str) -> bool:
        """Manually evict a non-pinned session"""
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
        """Single long-running worker: drain `_evict_signal`, run"""
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
        """Periodic background snapshot writer."""
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
            if (now - st.last_event_at) < idle_s:
                continue
            ripe.append(sid)
        return ripe

    async def _snapshot_one(self, sid: str) -> None:
        """Build a snapshot from live state (sync, atomic on the loop)"""
        state = self._sessions.get(sid)
        if state is None or state.closed:
            return
        snap_seq = state.last_seq
        snap = build_snapshot(state)
        session_dir = self._session_dir(sid)
        await asyncio.to_thread(write_snapshot, session_dir, snap)
        state.last_snapshot_seq = snap_seq


    def session_lock(self, sid: str) -> asyncio.Lock:
        """Return the asyncio.Lock for `sid`, allocating it on first"""
        return self._per_session_locks.setdefault(sid, asyncio.Lock())

    async def delete(
        self, sid: str, *, force: bool = False,
    ) -> bool:
        """Delete a session: drop in-memory state, flush pending events,"""
        async with self._sessions_lock:
            state = self._sessions.get(sid)
            if state is not None:
                if state.pinned and not force:
                    return False
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
        """Remove a session dir entirely. Tolerates missing files,"""
        import shutil
        if sdir.exists():
            shutil.rmtree(sdir, ignore_errors=False)

    async def delete_for_app(self, app_id: str) -> int:
        """Delete every session owned by `app_id`"""
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
        """Return all session_ids belonging to `app_id`. Combines the"""
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
        """Filesystem fallback: walk `self._root` reading meta.json"""
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
        """Return a list of `SessionSummary` for every session of"""
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
        """Return the user_id that owns `(app_id, sid)`, irrespective"""
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
        """Boot-time recovery: walk the sessions root, find sessions"""
        marked = 0
        if not self._root.exists():
            return marked
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
        """Sync helper for recover_orphans: walk the sessions root,"""
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
        """p50/p95/p99 over the last ~1024 `append_event` samples."""
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
