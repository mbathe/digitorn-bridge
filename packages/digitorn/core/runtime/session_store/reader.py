"""High-level read helpers for callers that don't need the full
``SessionStore`` API.

Thin wrappers over ``InMemorySessionStore`` that mirror common queries
the daemon makes today via Postgres ``SELECT ... FROM history_log``:

  * ``load_messages_for_llm(sid)`` -- chat-completion-shaped messages
    list (role + content + tool_calls), ready to pass to LiteLLM.
    Replaces the legacy ``SELECT messages WHERE session_id=...``.
  * ``load_snapshot(sid)``         -- snapshot.json fast-reopen path.
  * ``replay_events_since(sid, n)`` -- async iterator over events with
    seq > n, for SSE reconnect.

Designed to feel like a stdlib query helper. The agent loop / manager
module can swap a DB call for one of these with a one-line change.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from digitorn.core.runtime.session_store.session_state import SessionState
from digitorn.core.runtime.session_store.store import InMemorySessionStore
from digitorn.core.runtime.session_store.types import Event

logger = logging.getLogger(__name__)


def load_messages_for_llm(
    store: InMemorySessionStore,
    sid: str,
) -> list[dict[str, Any]]:
    """Return the chat-completion-shaped message list the LLM expects.

    Reads from ``state.messages`` directly (in-memory dict access,
    sub-microsecond). The session must be opened first via
    ``store.open(sid, ...)`` -- callers that just want to peek at a
    cold session should use ``load_snapshot`` instead, which doesn't
    require pinning the session in cache.
    """
    state: SessionState | None = store.state(sid)
    if state is None:
        return []
    out: list[dict[str, Any]] = []
    for m in state.messages:
        row: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            row["tool_calls"] = m.tool_calls
        out.append(row)
    return out


async def load_snapshot(
    store: InMemorySessionStore,
    sid: str,
) -> dict[str, Any] | None:
    """Read the snapshot.json. Used by the reopen-chat UX path."""
    return await store.read_snapshot(sid)


async def replay_events_since(
    store: InMemorySessionStore,
    sid: str,
    *,
    since: int = 0,
) -> AsyncIterator[Event]:
    """Async iterator over events with seq > ``since``. Used by the
    SSE reconnect path to stream missed events to a client."""
    async for ev in store.stream_events(sid, since=since):
        yield ev


def latest_seq(store: InMemorySessionStore, sid: str) -> int:
    """Sub-microsecond read of the per-session high-water-mark seq.
    Used by clients on first connect to know where to start streaming.
    """
    state = store.state(sid)
    return state.last_seq if state is not None else 0


def session_summary(
    store: InMemorySessionStore, sid: str,
) -> dict[str, Any] | None:
    """Tiny summary object: ~150 bytes. Listing pages, sidebars, etc."""
    state = store.state(sid)
    if state is None:
        return None
    return state.summary()
