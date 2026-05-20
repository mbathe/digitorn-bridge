"""High-level read helpers for callers that don't need the full"""

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
    """Return the chat-completion-shaped message list the LLM expects."""
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
    """Async iterator over events with seq > `since`. Used by the"""
    async for ev in store.stream_events(sid, since=since):
        yield ev


def latest_seq(store: InMemorySessionStore, sid: str) -> int:
    """Sub-microsecond read of the per-session high-water-mark seq."""
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
