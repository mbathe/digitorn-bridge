"""SessionState: the in-memory canonical state for one chat session."""

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
    goal: str = ""
    semantic_facts: list[dict[str, Any]] = field(default_factory=list)
    workspace_files: dict[str, FileState] = field(default_factory=dict)
    behavior_state: dict[str, object] = field(default_factory=dict)

    children: list[ChildAgentRef] = field(default_factory=list)
    blobs: dict[str, BlobRef] = field(default_factory=dict)

    started_at: str = field(default_factory=utc_iso)
    ended_at: str | None = None
    closed: bool = False

    title: str = ""
    turn_count: int = 0
    workspace: str = ""
    workdir: str = ""
    interrupted: bool = False
    interrupted_at: str | None = None
    active_mode_id: str | None = None

    pinned: bool = False
    last_accessed_at: float = field(default_factory=time.monotonic)
    last_event_at: float = field(default_factory=time.monotonic)
    bytes_estimate: int = 0

    applied_compaction: Compaction | None = None

    cost_total: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0

    streaming_partials: dict[int, str] = field(default_factory=dict)

    def event_count(self) -> int:
        return len(self.events)

    def touch(self) -> None:
        self.last_accessed_at = time.monotonic()

    def summary(self) -> dict[str, object]:
        """Summary line for the session index. Stable, JSON-friendly,"""
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
