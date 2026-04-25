"""Per-session state for the preview module.

Stores everything the web UI needs to reconstruct its current view when
it (re)connects: the canvas graph (nodes + edges), a key-value state
map, and a ring buffer of recent events.

The in-memory store can be augmented with a ``loader`` callback that
hydrates a session from durable storage (DB) the first time it is
accessed. This is what turns "close + reopen a session" into a no-loss
round-trip — the snapshot persisted by the preview module's debounced
flush is replayed into a fresh ``PreviewSessionState`` on demand.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


_MAX_EVENTS = 500  # ring buffer cap per session


@dataclass
class PreviewNode:
    """A single node in the canvas graph (ReactFlow-compatible shape)."""

    id: str
    type: str = "default"
    label: str = ""
    position: dict[str, float] = field(default_factory=lambda: {"x": 0.0, "y": 0.0})
    data: dict[str, Any] = field(default_factory=dict)
    status: str = "idle"  # idle | running | done | error
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "position": self.position,
            "data": self.data,
            "status": self.status,
            "updated_at": self.updated_at,
        }


@dataclass
class PreviewEdge:
    """A connection between two nodes (ReactFlow-compatible shape)."""

    id: str
    source: str
    target: str
    label: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "label": self.label,
            "data": self.data,
        }


@dataclass
class PreviewEvent:
    """An event pushed by the agent — consumed by ``usePreviewEvents``."""

    seq: int
    event_type: str
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
class PreviewSessionState:
    """All preview data for a single session.

    The data model is generic: a key/value ``state`` map plus arbitrary
    ``resources`` partitioned into named channels. App shells decide
    what each channel holds — canvas nodes, source files, slides,
    spreadsheet cells, document blocks. The module never inspects
    payloads; it only stores, fan-outs, and replays them.
    """

    session_id: str
    user_id: str | None = None
    state: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    events: deque[PreviewEvent] = field(
        default_factory=lambda: deque(maxlen=_MAX_EVENTS)
    )
    _seq: int = 0

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def channel(self, name: str) -> dict[str, dict[str, Any]]:
        ch = self.resources.get(name)
        if ch is None:
            ch = {}
            self.resources[name] = ch
        return ch

    def snapshot(self) -> dict[str, Any]:
        """Full state replay — sent on new connection."""
        return {
            "session_id": self.session_id,
            "state": dict(self.state),
            "resources": {
                name: {rid: dict(payload) for rid, payload in items.items()}
                for name, items in self.resources.items()
            },
            "nodes": [
                dict(p) for p in self.resources.get("nodes", {}).values()
            ],
            "edges": [
                dict(p) for p in self.resources.get("edges", {}).values()
            ],
            "events": [e.to_dict() for e in self.events],
            "seq": self._seq,
        }

    def clear(self) -> None:
        self.state.clear()
        self.resources.clear()
        self.events.clear()
        self._seq = 0

    def restore_from_dict(self, data: dict[str, Any]) -> None:
        """Hydrate this state from a persisted snapshot dict.

        Shape expected (matches :meth:`snapshot` output):
            {
              "state": {...},
              "resources": {channel: {id: payload}},
              "seq": int,
              "user_id": str | None,
            }

        ``events`` is NOT restored — the ring buffer only exists for
        short-term replay. After a DB reload the client receives a
        fresh ``preview:snapshot`` event carrying the current state;
        old events are not useful.
        """
        self.state = dict(data.get("state") or {})
        self.resources = {
            ch: {rid: dict(payload) for rid, payload in items.items()}
            for ch, items in (data.get("resources") or {}).items()
        }
        self._seq = int(data.get("seq") or 0)
        if data.get("user_id"):
            self.user_id = data["user_id"]
        self.events.clear()


Loader = Callable[[str], Awaitable[dict[str, Any] | None]]


class PreviewSessionStore:
    """Process-wide map of session_id → PreviewSessionState.

    Thread-safe in the asyncio sense: all access happens on the event
    loop, so no locks are needed.

    When constructed with a ``loader`` callback, the first access to an
    unknown ``session_id`` triggers an async DB lookup — if a durable
    snapshot exists the state is rehydrated; otherwise a blank state is
    created. The loader is async, so callers that know they're on the
    event loop should use :meth:`get_or_create_async`.
    """

    def __init__(self, loader: Loader | None = None) -> None:
        self._sessions: dict[str, PreviewSessionState] = {}
        self._loader = loader

    def get_or_create(self, session_id: str) -> PreviewSessionState:
        """Synchronous path — returns blank state if not already cached.

        Kept for code paths that don't have a live event loop (tests,
        CLI-standalone). For the main daemon flow, callers that want DB
        hydration should use :meth:`get_or_create_async` once per
        session — e.g. the agent loop calls it at turn start.
        """
        if session_id not in self._sessions:
            self._sessions[session_id] = PreviewSessionState(session_id=session_id)
        return self._sessions[session_id]

    async def get_or_create_async(self, session_id: str) -> PreviewSessionState:
        """Hydrate from DB (via ``loader``) if we haven't seen this session yet."""
        if session_id in self._sessions:
            return self._sessions[session_id]
        state = PreviewSessionState(session_id=session_id)
        if self._loader is not None:
            try:
                data = await self._loader(session_id)
                if data:
                    state.restore_from_dict(data)
                    logger.info(
                        "preview_state_restored sid=%s resources=%d state_keys=%d seq=%d",
                        session_id,
                        sum(len(v) for v in state.resources.values()),
                        len(state.state),
                        state._seq,
                    )
            except Exception as exc:
                logger.warning(
                    "preview_state_restore_failed sid=%s: %s", session_id, exc,
                )
        self._sessions[session_id] = state
        return state

    def get(self, session_id: str) -> PreviewSessionState | None:
        return self._sessions.get(session_id)

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def session_ids(self) -> list[str]:
        return list(self._sessions.keys())
