"""Apply an Event to a SessionState's live projections.

Critical invariants:

  * The ``events`` journal is updated by ``InMemorySessionStore``
    BEFORE ``apply_projection`` is called. Projections are derived
    views; the journal is the source of truth.
  * No event type is filtered out of the journal. Some types simply
    have no projection effect (token, thinking_delta, heartbeat) and
    fall through here as no-ops.
  * Projections are idempotent under replay: rebuilding from
    ``events.jsonl`` from seq=0 produces the same final state.
"""

from __future__ import annotations

from typing import Any

from digitorn.core.runtime.session_store.session_state import SessionState
from digitorn.core.runtime.session_store.types import (
    ApprovalRequest,
    ChildAgentRef,
    Event,
    FileState,
    Message,
    Todo,
    ToolCall,
    ToolResult,
)


# Event types that are journal-only, no projection update needed.
# Streaming deltas and lifecycle markers fall here. Listed
# explicitly so a typo in event.type doesn't silently no-op.
_NO_PROJECTION_TYPES = frozenset({
    "token",
    "thinking_delta",
    "thinking_started",
    "thinking_stopped",
    "out_token",
    "in_token",
    "tool_call_streaming",
    "stream_done",
    "turn:heartbeat",
    "turn:start",
    "turn:end",
    "message_started",
    "message_done",
    "hook",
    "behavior:warning",
    "behavior:remind",
    "preview:delta",
    "preview:state",
    "agent_progress",
    "agent_event",
})


def apply_projection(state: SessionState, ev: Event) -> None:
    """Mutate ``state``'s live projections to reflect ``ev``.

    Runs inline with the ``events.append(ev)`` so callers see a
    consistent snapshot. Single-writer per session is the contract.
    """
    t = ev.type

    if t in _NO_PROJECTION_TYPES:
        return

    if t == "user_message":
        # Bridge-routed events carry the message text in
        # ``ev.payload["content"]`` (the live SessionBus envelope
        # shape). Direct ``history.record`` calls also set
        # ``ev.content`` at the top level. Read both so the
        # projection is shape-agnostic.
        msg_text = ev.content or str(ev.payload.get("content", "") or "")
        state.messages.append(Message(
            role="user",
            content=msg_text,
            seq=ev.seq,
            ts=ev.ts,
            attachments=_extract_attachments(ev),
        ))
        # Phase 1 projection: derive title from the very first user
        # message. Mirrors the legacy ConversationSession.add_user
        # behaviour. Once set, stays stable -- a user can rename via
        # an explicit ``session_title`` event handled below.
        if not state.title and msg_text:
            state.title = msg_text.strip()[:80]
        # An incoming user_message always clears any prior interrupted
        # flag: the user is back, the session is live again.
        if state.interrupted:
            state.interrupted = False
            state.interrupted_at = None
    elif t == "assistant_message":
        # Same payload-vs-top-level reading rule as ``user_message``.
        msg_text = ev.content or str(ev.payload.get("content", "") or "")
        state.messages.append(Message(
            role="assistant",
            content=msg_text,
            tool_calls=ev.tool_calls or [],
            seq=ev.seq,
            ts=ev.ts,
            attachments=_extract_attachments(ev),
        ))
        state.tokens_out += int(ev.payload.get("completion_tokens", 0) or 0)
        state.tokens_in += int(ev.payload.get("prompt_tokens", 0) or 0)
        state.cost_total += float(ev.payload.get("cost", 0.0) or 0.0)
        # Final assistant text landed durably -- the streaming partial
        # for this agent-slot is now stale; drop it so the bg buffer
        # doesn't accumulate forever and the cold-reload path can tell
        # apart "stream finished cleanly" from "crashed mid-stream".
        _slot = ev.payload.get("agent_seq")
        if isinstance(_slot, int):
            state.streaming_partials.pop(_slot, None)
    elif t == "assistant_message_partial":
        # Crash-recovery snapshot of an in-flight assistant stream.
        # The agent loop fires these via ``upsert_streaming_assistant``
        # roughly once per chunk batch; each one carries the FULL
        # text streamed so far for the agent-slot seq. Cleared when
        # the final ``assistant_message`` event lands for the same
        # slot. NOT appended to ``state.messages`` -- the live view
        # already shows the stream via tokens on the bus; the buffer
        # only matters when the daemon dies before the final flush.
        _slot = ev.payload.get("agent_seq")
        _partial = ev.content or str(ev.payload.get("content", "") or "")
        if isinstance(_slot, int):
            state.streaming_partials[_slot] = _partial
    elif t == "turn_terminal":
        # Phase 1 projection: ``turn_terminal`` is the canonical
        # end-of-turn signal in the new event-sourced flow. Fires on
        # success AND on abort/error so ``turn_count`` reflects every
        # attempted turn (matches the legacy
        # ConversationSession.turn_count++ semantics in manager_v2).
        # ``assistant_message`` is the legacy bus event -- we
        # intentionally do NOT increment there to avoid double-counting
        # when both are emitted in the same turn.
        state.turn_count += 1
    elif t == "session_title":
        # Phase 1: explicit title-set event (used by
        # ``maybe_update_session_title`` once it migrates off the
        # old SessionStore.put path). Idempotent.
        new_title = str(ev.payload.get("title", "") or ev.content or "").strip()
        if new_title:
            state.title = new_title[:200]
    elif t == "abort":
        # Phase 1 projection: abort marks the session interrupted so
        # the resume path can synthesise "interrupted" tool_results
        # for orphan tool_calls. Cleared on the next user_message.
        state.interrupted = True
        state.interrupted_at = ev.ts
        # The aborted turn's streaming buffer is no longer recoverable
        # via the normal assistant_message path; clear it so it does
        # not accumulate forever (one stale entry per abort over the
        # session lifetime). Replay path: ``upsert_streaming_assistant``
        # only writes to HistoryLog on abort finalization; the partial
        # text is still on disk for forensic recovery if needed.
        state.streaming_partials.clear()
    elif t == "session_workspace":
        # Phase 1: explicit workspace/workdir set event emitted at
        # session create. Harmless when absent (legacy sessions
        # filled them via the OLD SessionStore.put path).
        ws = str(ev.payload.get("workspace", "") or "")
        wd = str(ev.payload.get("workdir", "") or "")
        if ws:
            state.workspace = ws
        if wd:
            state.workdir = wd
    elif t == "system_message":
        msg_text = ev.content or str(ev.payload.get("content", "") or "")
        state.messages.append(Message(
            role="system",
            content=msg_text,
            seq=ev.seq, ts=ev.ts,
        ))
    elif t == "tool_call":
        tc_id = ev.tool_call_id or str(ev.payload.get("id", ""))
        if tc_id:
            state.tool_calls[tc_id] = ToolCall(
                id=tc_id,
                name=ev.name or str(ev.payload.get("name", "")),
                arguments=ev.payload.get("arguments") or {},
                status="pending",
                started_at=ev.ts,
            )
    elif t == "tool_result":
        tc_id = ev.tool_call_id or str(ev.payload.get("tool_call_id", ""))
        if tc_id:
            result = ToolResult(
                tool_call_id=tc_id,
                output=ev.payload.get("output"),
                success=bool(ev.success),
                error=ev.payload.get("error") or None,
                completed_at=ev.ts,
            )
            state.tool_results[tc_id] = result
            existing = state.tool_calls.get(tc_id)
            if existing is not None:
                existing.status = "completed" if ev.success else "failed"
    elif t == "approval_request":
        ar_id = str(ev.payload.get("id") or ev.tool_call_id or "")
        if ar_id:
            state.pending_approvals[ar_id] = ApprovalRequest(
                id=ar_id,
                kind=str(ev.payload.get("kind", "tool_call")),
                payload=dict(ev.payload),
                created_at=ev.ts,
                status="pending",
            )
    elif t == "approval_resolved":
        ar_id = str(ev.payload.get("id") or "")
        if ar_id:
            state.pending_approvals.pop(ar_id, None)
    elif t == "todo_add":
        todo_id = str(ev.payload.get("id") or "")
        if todo_id:
            state.todos.append(Todo(
                id=todo_id,
                text=str(ev.payload.get("text", "")),
                status=str(ev.payload.get("status", "pending")),
                created_at=ev.ts,
                updated_at=ev.ts,
            ))
    elif t == "todo_update":
        todo_id = str(ev.payload.get("id") or "")
        for todo in state.todos:
            if todo.id == todo_id:
                if "status" in ev.payload:
                    todo.status = str(ev.payload["status"])
                if "text" in ev.payload:
                    todo.text = str(ev.payload["text"])
                todo.updated_at = ev.ts
                break
    elif t == "memory_remember":
        key = str(ev.payload.get("key", ""))
        if key:
            state.memory_facts[key] = str(ev.payload.get("value", ""))
    elif t == "memory_forget":
        key = str(ev.payload.get("key", ""))
        if key:
            state.memory_facts.pop(key, None)
    elif t == "workspace_write" or t == "workspace_edit":
        path = str(ev.payload.get("path") or ev.target_resource or "")
        if path:
            state.workspace_files[path] = FileState(
                path=path,
                content_hash=str(ev.payload.get("content_hash", "")),
                baseline_hash=ev.payload.get("baseline_hash"),
                status=str(ev.payload.get("validation", "approved")),
                bytes=int(ev.payload.get("bytes", 0) or 0),
            )
    elif t == "workspace_delete":
        path = str(ev.payload.get("path") or ev.target_resource or "")
        if path:
            state.workspace_files.pop(path, None)
    elif t == "agent_spawn":
        run_id = str(ev.payload.get("run_id") or "")
        if run_id:
            state.children.append(ChildAgentRef(
                run_id=run_id,
                kind=str(ev.payload.get("specialist") or ev.payload.get("kind") or ""),
                spawned_at=ev.ts,
            ))
    elif t == "agent_result":
        run_id = str(ev.payload.get("run_id") or "")
        for child in state.children:
            if child.run_id == run_id:
                child.completed_at = ev.ts
                child.status = "completed" if ev.success else "failed"
                child.result_summary = str(ev.payload.get("summary", "")) or None
                break
    elif t == "agent_cancel":
        run_id = str(ev.payload.get("run_id") or "")
        for child in state.children:
            if child.run_id == run_id:
                child.completed_at = ev.ts
                child.status = "cancelled"
                break
    elif t == "session:close":
        state.closed = True
        state.ended_at = ev.ts


def _extract_attachments(ev: Event) -> list:
    raw = ev.payload.get("attachments") if ev.payload else None
    if not raw:
        return []
    out: list = []
    for item in raw:
        if isinstance(item, dict) and "hash" in item:
            from digitorn.core.runtime.session_store.types import BlobRef
            out.append(BlobRef.from_dict(item))
    return out
