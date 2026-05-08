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
from collections import OrderedDict
from pathlib import Path
from typing import AsyncIterator, Iterable

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
    ) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._sessions: "OrderedDict[str, SessionState]" = OrderedDict()
        self._sessions_lock = asyncio.Lock()
        self._max_sessions = max_sessions_in_memory
        self._max_bytes = max_bytes_in_memory
        self._current_bytes = 0
        self._index = index

        self._allocator = SeqAllocator(seed_loader=self._seed_seq_from_disk)
        self._flusher = DiskFlusher(
            session_dir_resolver=self._session_dir,
            flush_interval_ms=flush_interval_ms,
        )

    async def start(self) -> None:
        await self._flusher.start()

    async def stop(self) -> None:
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
        so an index hiccup never breaks the agent loop."""
        if self._index is None:
            return
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
        """
        state = self._sessions.get(sid)
        if state is None:
            raise KeyError(
                f"session_not_open: {sid} -- call open() before append_event()"
            )

        event.seq = self._allocator.next(
            user_id=state.user_id, session_id=sid,
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
        self._sessions.move_to_end(sid)

        self._flusher.enqueue(sid, event)

        if self._current_bytes > self._max_bytes \
                or len(self._sessions) > self._max_sessions:
            asyncio.create_task(self._maybe_evict())

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
        await asyncio.to_thread(self._persist_close, sid, state)
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
        await asyncio.to_thread(
            write_snapshot, self._session_dir(sid), build_snapshot(state),
        )
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
            return True

    def stats(self) -> dict:
        return {
            "sessions_in_memory": len(self._sessions),
            "current_bytes": self._current_bytes,
            "max_bytes": self._max_bytes,
            "max_sessions": self._max_sessions,
            "flusher_written": self._flusher.written,
            "flusher_dropped": self._flusher.dropped,
            "flusher_batches": self._flusher.batch_count,
        }
