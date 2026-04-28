"""Per-session widget runtime store - mirrors PreviewSessionStore.

Holds, for each ``session_id``:

- A map of mounted widgets (``widget_id`` → tree, ctx, zone)
- A monotonic seq counter so clients can dedupe on reconnect
- A persistent global state map (the ``state.*`` scope, scope=global)
- A subscriber fan-out queue list

Nothing is persisted to disk by default. The ``state.global`` scope
is mirrored to the daemon's KV store via the API routes if needed.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


_MAX_EVENTS = 500


@dataclass
class MountedWidget:
    """One widget mounted in a session - tracked so updates / close
    can find it by ``widget_id``.
    """

    widget_id: str
    zone: str  # inline | chat_side | workspace | modal
    target: str | None = None
    ref: str | None = None
    tree: dict[str, Any] | None = None
    ctx: dict[str, Any] = field(default_factory=dict)
    turn_id: str | None = None
    mounted_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "widget_id": self.widget_id,
            "zone": self.zone,
            "target": self.target,
            "ref": self.ref,
            "tree": self.tree,
            "ctx": dict(self.ctx),
            "turn_id": self.turn_id,
            "mounted_at": self.mounted_at,
        }


@dataclass
class WidgetEvent:
    """One delta event pushed to subscribers."""

    seq: int
    event_type: str  # widget:render | widget:update | widget:close | widget:error
    data: dict[str, Any]
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_type": self.event_type,
            "data": self.data,
            "timestamp": self.timestamp,
        }


@dataclass
class WidgetSessionState:
    """Everything the ``widget`` module tracks for one session."""

    session_id: str
    mounted: dict[str, MountedWidget] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)  # global scope
    events: deque[WidgetEvent] = field(
        default_factory=lambda: deque(maxlen=_MAX_EVENTS)
    )
    _seq: int = 0

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "mounted": [w.to_dict() for w in self.mounted.values()],
            "state": dict(self.state),
            "events": [e.to_dict() for e in self.events],
            "seq": self._seq,
        }

    def clear(self) -> None:
        self.mounted.clear()
        self.state.clear()
        self.events.clear()
        self._seq = 0


class WidgetSessionStore:
    """Process-wide store of per-session widget state."""

    def __init__(self) -> None:
        self._sessions: dict[str, WidgetSessionState] = {}

    def get_or_create(self, session_id: str) -> WidgetSessionState:
        if session_id not in self._sessions:
            self._sessions[session_id] = WidgetSessionState(session_id=session_id)
        return self._sessions[session_id]

    def get(self, session_id: str) -> WidgetSessionState | None:
        return self._sessions.get(session_id)

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
