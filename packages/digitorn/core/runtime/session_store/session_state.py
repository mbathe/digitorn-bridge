"""SessionState: the in-memory canonical state for one chat session.

Holds:
  * ``events`` -- the FULL append-only journal (every event, including
    every streaming chunk -- token, thinking_delta, heartbeat, hook,
    tool_call_streaming, ...). This is the authoritative log.
  * Live projections (messages, tool_calls, todos, ...) -- cached
    views the agent loop reads in O(1). Anyone can rebuild them by
    replaying ``events`` from seq=0; if a projection ever diverges
    from the journal, the journal wins.
  * Cache management metadata (last_accessed_at, bytes_estimate,
    pinned, closed).

Thread-safety: mutations happen on the daemon's main asyncio loop or
the persist worker thread. The internal list/dict ops are GIL-atomic
in CPython, so a single-writer-per-session contract is sufficient and
no per-state lock is needed for the canonical structures. The
``InMemorySessionStore`` enforces single-writer-per-session at append
time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from digitorn.core.runtime.session_store.types import (
    ApprovalRequest,
    BlobRef,
    ChildAgentRef,
    Event,
    FileState,
    Message,
    ParentLink,
    Todo,
    ToolCall,
    ToolResult,
    utc_iso,
)
from digitorn.core.runtime.session_store.compaction import Compaction


@dataclass
class SessionState:
    """In-memory canonical state for one session."""

    session_id: str
    app_id: str
    user_id: str

    parent_link: ParentLink | None = None

    events: list[Event] = field(default_factory=list)
    last_seq: int = 0
    first_seq: int = 0
    last_flushed_seq: int = 0
    last_snapshot_seq: int = 0

    messages: list[Message] = field(default_factory=list)
    tool_calls: dict[str, ToolCall] = field(default_factory=dict)
    tool_results: dict[str, ToolResult] = field(default_factory=dict)
    pending_approvals: dict[str, ApprovalRequest] = field(default_factory=dict)
    todos: list[Todo] = field(default_factory=list)
    memory_facts: dict[str, str] = field(default_factory=dict)
    workspace_files: dict[str, FileState] = field(default_factory=dict)
    behavior_state: dict[str, object] = field(default_factory=dict)

    children: list[ChildAgentRef] = field(default_factory=list)
    blobs: dict[str, BlobRef] = field(default_factory=dict)

    started_at: str = field(default_factory=utc_iso)
    ended_at: str | None = None
    closed: bool = False

    # Chat-level metadata absorbed from the legacy ConversationSession
    # so the new SessionStore is the SINGLE source of truth (Phase 1
    # of the SessionStore-unification refactor).
    #
    # ``title``         : derived projection from the first user_message
    #                     (~80 char prefix) -- powers the sidebar list.
    # ``turn_count``    : incremented on every ``assistant_message`` event.
    # ``workspace``     : daemon-private per-session dir under
    #                     ``~/.digitorn/workspaces/{app}/{sid}/``. Where
    #                     state.json + baselines + hidden ``__sdk__/``
    #                     live. Stamped at session create.
    # ``workdir``       : the agent's working directory. Defaults to
    #                     ``workspace`` when the app's ``runtime.workdir_mode``
    #                     is ``none`` (the typical case). When the app
    #                     declares ``required``, ``workdir`` is the
    #                     user-provided path passed at session create.
    # ``interrupted``   : True when the session ended via abort instead
    #                     of natural turn completion -- enables smart
    #                     resume (synthesized "interrupted" tool_results
    #                     fill the orphan tool_call gaps).
    # ``interrupted_at``: ISO timestamp of the most recent abort.
    title: str = ""
    turn_count: int = 0
    workspace: str = ""
    workdir: str = ""
    interrupted: bool = False
    interrupted_at: str | None = None

    pinned: bool = False
    last_accessed_at: float = field(default_factory=time.monotonic)
    # Phase 6: separate idle-clock for the bg snapshot worker. Only
    # ``append_event`` advances this; reads (open/get) do NOT. Avoids a
    # busy-read pattern from keeping the session permanently "active"
    # in the eyes of the snapshot worker.
    last_event_at: float = field(default_factory=time.monotonic)
    bytes_estimate: int = 0

    # Latest compaction applied to this session, or None if no
    # compaction has run yet. When set: state.events / state.messages
    # are bounded post-cutoff, the small projections (todos, memory,
    # workspace, ...) are loaded from snapshot.json and kept full.
    applied_compaction: Compaction | None = None

    cost_total: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0

    def event_count(self) -> int:
        return len(self.events)

    def touch(self) -> None:
        self.last_accessed_at = time.monotonic()

    def summary(self) -> dict[str, object]:
        """Summary line for the session index. Stable, JSON-friendly,
        ~150 bytes typical."""
        return {
            "session_id": self.session_id,
            "app_id": self.app_id,
            "user_id": self.user_id,
            "parent_session_id": (
                self.parent_link.parent_session_id if self.parent_link else None
            ),
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "event_count": len(self.events),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "closed": self.closed,
            "cost_total": self.cost_total,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "child_count": len(self.children),
            "title": self.title,
            "turn_count": self.turn_count,
            "interrupted": self.interrupted,
        }
