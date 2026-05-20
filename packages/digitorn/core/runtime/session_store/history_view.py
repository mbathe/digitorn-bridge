"""Render a SessionState as the legacy `/history` API payload."""

from __future__ import annotations

import bisect
from typing import Any

from digitorn.core.runtime.session_store.session_state import SessionState
from digitorn.core.runtime.session_store.types import Event, Message


_REPLAY_NOISE_TYPES = frozenset({
    "token", "thinking_delta",
    "out_token", "in_token",
    "tool_call_streaming", "stream_done",
    "turn:heartbeat",
    "preview:delta", "preview:state",
    "agent_progress",
    "behavior:warning", "behavior:remind",
    "hook",
    "assistant_message_partial",
})


def _message_to_dict(msg: Message) -> dict[str, Any]:
    """Render a projected Message as the legacy `messages[]` shape:"""
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
    """Render an Event as the legacy `events[]` shape. Promotes"""
    payload = dict(ev.payload or {})
    return {
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
    """Return state.messages projected as the legacy `messages[]`"""
    out: list[dict[str, Any]] = []
    for msg in state.messages:
        if not include_system and msg.role == "system":
            continue
        out.append(_message_to_dict(msg))
    out.sort(key=lambda m: m["seq"])
    return out


def _filter_events(events: list[Event]) -> list[Event]:
    return [e for e in events if e.type not in _REPLAY_NOISE_TYPES]


def _index_seq_array(events: list[Event]) -> list[int]:
    """Build the sorted seq array used by `bisect` for O(log N)"""
    return [e.seq for e in events]


def paginate_events_forward(
    state: SessionState, *,
    since_seq: int = 0,
    limit: int = 50000,
) -> tuple[list[dict[str, Any]], int, int, bool]:
    """Forward pagination: events with seq > since_seq, capped at limit."""
    filtered = _filter_events(state.events)
    total = len(filtered)
    seqs = _index_seq_array(filtered)
    start = bisect.bisect_right(seqs, int(since_seq or 0))
    page = filtered[start:start + max(int(limit), 0)]
    rendered = [_event_to_dict(e) for e in page]
    if rendered:
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
    """Backward pagination: most recent `limit` events with seq <"""
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
    """One-shot helper for `GET /sessions/{sid}/history`"""
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

    filtered = _filter_events(state.events)
    oldest_seq = int(filtered[0].seq) if filtered else 0
    current_seq = int(state.last_seq)
    client_since = int(since_seq or 0)
    truncated = (
        client_since > 0
        and oldest_seq > 0
        and oldest_seq > client_since + 1
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
        "current_seq": current_seq,
        "oldest_seq": oldest_seq,
        "truncated": truncated,
        "streaming_partials": dict(state.streaming_partials)
            if state.streaming_partials else {},
    }
