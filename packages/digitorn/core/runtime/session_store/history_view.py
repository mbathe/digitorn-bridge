"""Render a SessionState as the legacy ``/history`` API payload.

The OLD path queried Postgres ``history_log`` for two parallel streams:
``messages`` (kind='message' rows) and ``events`` (kind='event' rows),
with cursor-based pagination by seq. This module produces the same
shape from the in-memory SessionState the new SessionStore maintains.

Strict ordering: every output entry carries ``seq``, sorted ascending.
Pagination is O(log N) via ``bisect`` on the events list (it's already
in seq order by SeqAllocator construction).

No duplicate suppression beyond what apply_projection already does -
state.messages is the canonical projection of user/assistant turns.
"""

from __future__ import annotations

import bisect
from typing import Any

from digitorn.core.runtime.session_store.session_state import SessionState
from digitorn.core.runtime.session_store.types import Event, Message


# Streaming/lifecycle deltas that the live UI already animated. Excluded
# from replay because their effect is captured in the assembled
# assistant_message + tool_call/tool_result events that DO appear.
_REPLAY_NOISE_TYPES = frozenset({
    "token", "thinking_delta",
    "out_token", "in_token",
    "tool_call_streaming", "stream_done",
    "turn:heartbeat",
    "preview:delta", "preview:state",
    "agent_progress",
    "behavior:warning", "behavior:remind",
    "hook",
})


def _message_to_dict(msg: Message) -> dict[str, Any]:
    """Render a projected Message as the legacy ``messages[]`` shape:
    role, content, seq, tool_call_id, tool_calls, optional thinking."""
    return {
        "role": msg.role,
        "content": msg.content,
        "seq": int(msg.seq),
        "tool_call_id": None,
        "tool_calls": list(msg.tool_calls or []),
        "ts": msg.ts,
        "attachments": [a.to_dict() for a in (msg.attachments or [])],
    }


def _event_to_dict(ev: Event) -> dict[str, Any]:
    """Render an Event as the legacy ``events[]`` shape. Promotes
    contract fields (event_id / op_id / op_type / op_state) from
    payload to envelope top-level so the client reducer reads them
    directly. Mirrors the legacy DB row mapping."""
    payload = dict(ev.payload or {})
    return {
        # The legacy contract used the DB row id; the new store has no
        # row id. ``seq`` is unique-per-session and monotone, which is
        # what every dedup/sort key in the client actually relies on.
        "id": int(ev.seq),
        "ts": ev.ts,
        "seq": int(ev.seq),
        "kind": payload.get("event_kind") or ev.kind,
        "type": ev.type,
        "event_id": payload.get("event_id") or "",
        "op_id": payload.get("op_id") or "",
        "op_type": payload.get("op_type") or "",
        "op_state": payload.get("op_state") or "",
        "session_id": ev.session_id,
        "user_id": ev.user_id,
        "role": ev.role,
        "content": ev.content,
        "tool_call_id": ev.tool_call_id,
        "tool_calls": ev.tool_calls,
        "payload": payload,
        "correlation_id": ev.correlation_id or payload.get("correlation_id") or "",
    }


def render_messages(
    state: SessionState, *, include_system: bool = False,
) -> list[dict[str, Any]]:
    """Return state.messages projected as the legacy ``messages[]``
    array. When ``include_system=False``, system messages are dropped
    (matches legacy ``_build_history_turns`` behavior)."""
    out: list[dict[str, Any]] = []
    for msg in state.messages:
        if not include_system and msg.role == "system":
            continue
        out.append(_message_to_dict(msg))
    out.sort(key=lambda m: m["seq"])
    return out


def _filter_events(events: list[Event]) -> list[Event]:
    """Drop streaming-chunk noise that the UI already animated live."""
    return [e for e in events if e.type not in _REPLAY_NOISE_TYPES]


def _index_seq_array(events: list[Event]) -> list[int]:
    """Build the sorted seq array used by ``bisect`` for O(log N)
    pagination cursors. Cheap to recompute per request -- the events
    list is already small (at most a few thousand per session), and
    bisect needs an actual list anyway."""
    return [e.seq for e in events]


def paginate_events_forward(
    state: SessionState, *,
    since_seq: int = 0,
    limit: int = 50000,
) -> tuple[list[dict[str, Any]], int, int, bool]:
    """Forward pagination: events with seq > since_seq, capped at limit.

    Returns ``(events, total, next_seq, has_more)``:
      * ``events``  - rendered legacy event dicts, ASC by seq
      * ``total``   - total events for this session (after noise filter)
      * ``next_seq`` - cursor for the next page (last_seq + 1)
      * ``has_more`` - True iff more events past this page exist
    """
    filtered = _filter_events(state.events)
    total = len(filtered)
    seqs = _index_seq_array(filtered)
    start = bisect.bisect_right(seqs, int(since_seq or 0))
    page = filtered[start:start + max(int(limit), 0)]
    rendered = [_event_to_dict(e) for e in page]
    if rendered:
        # Cursor contract: ``next_seq`` is the highest seq the caller
        # has consumed. The filter is strict ``seq > since_seq`` (via
        # bisect_right), so passing ``last_seq`` back returns events
        # starting at ``last_seq + 1`` -- contiguous, no skip.
        # NOTE: returning ``last_seq + 1`` AS the cursor caused a
        # one-event-per-page gap when the next event existed at exactly
        # ``last_seq + 1`` -- bisect_right skipped past it. This was the
        # silent bug inherited from the legacy SQL path.
        next_seq = int(rendered[-1]["seq"])
        has_more = (start + len(page)) < total
    else:
        next_seq = int(since_seq or 0)
        has_more = False
    return rendered, total, next_seq, has_more


def paginate_events_backward(
    state: SessionState, *,
    before_seq: int = 0,
    limit: int = 50000,
) -> tuple[list[dict[str, Any]], int, int, bool]:
    """Backward pagination: most recent ``limit`` events with seq <
    before_seq, then snap the page boundary to the latest
    ``user_message`` so rendered turns are always complete.

    ``before_seq=0`` means "start from the end" (newest first).

    Returns ``(events, total, prev_seq, has_more_back)``:
      * ``events``       - rendered legacy event dicts, ASC by seq
      * ``total``        - total events for this session
      * ``prev_seq``     - oldest seq in this page (cursor for next
                           backward call)
      * ``has_more_back``- True iff at least one event with seq <
                           prev_seq still exists
    """
    filtered = _filter_events(state.events)
    total = len(filtered)
    if total == 0:
        return [], 0, 0, False

    seqs = _index_seq_array(filtered)
    if int(before_seq or 0) > 0:
        upper_idx = bisect.bisect_left(seqs, int(before_seq))
    else:
        upper_idx = total

    take = max(int(limit), 0)
    naive_start = max(upper_idx - take, 0)
    if naive_start >= upper_idx:
        return [], total, 0, False

    raw_min = filtered[naive_start].seq

    # Snap to the user_message at or before raw_min so the page starts
    # at a turn boundary (mirrors the legacy DB query at lines 1510+).
    boundary_idx = naive_start
    for i in range(naive_start, -1, -1):
        if filtered[i].type == "user_message" and filtered[i].seq <= raw_min:
            boundary_idx = i
            break

    page = filtered[boundary_idx:upper_idx]
    rendered = [_event_to_dict(e) for e in page]
    prev_seq = int(rendered[0]["seq"]) if rendered else 0
    has_more_back = boundary_idx > 0
    return rendered, total, prev_seq, has_more_back


def render_history_payload(
    state: SessionState, *,
    include_system: bool = False,
    since_seq: int = 0,
    before_seq: int | None = None,
    events_limit: int = 50000,
) -> dict[str, Any]:
    """One-shot helper for ``GET /sessions/{sid}/history``. Returns the
    full legacy-shaped payload (messages + events + pagination cursors)
    derived entirely from in-memory ``SessionState``. Single read, no
    Postgres roundtrip, no duplicate suppression needed (state.messages
    is the canonical projection)."""
    messages = render_messages(state, include_system=include_system)
    if before_seq is not None:
        events, total, prev_seq, has_more_back = paginate_events_backward(
            state, before_seq=int(before_seq or 0), limit=int(events_limit),
        )
        next_seq = int(events[-1]["seq"]) + 1 if events else int(since_seq or 0)
        has_more = False  # backward mode owns has_more_back
    else:
        events, total, next_seq, has_more = paginate_events_forward(
            state, since_seq=int(since_seq or 0), limit=int(events_limit),
        )
        prev_seq = int(events[0]["seq"]) if events else 0
        has_more_back = (
            paginate_events_backward(
                state, before_seq=prev_seq, limit=1,
            )[3] if prev_seq > 0 else False
        )

    return {
        "messages": messages,
        "message_count": len(messages),
        "events": events,
        "event_count": len(events),
        "events_total": total,
        "events_next_seq": next_seq,
        "events_has_more": has_more,
        "events_prev_seq": prev_seq,
        "events_has_more_back": has_more_back,
    }
