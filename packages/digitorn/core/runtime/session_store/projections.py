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
        state.messages.append(Message(
            role="user",
            content=ev.content or "",
            seq=ev.seq,
            ts=ev.ts,
            attachments=_extract_attachments(ev),
        ))
    elif t == "assistant_message":
        state.messages.append(Message(
            role="assistant",
            content=ev.content or "",
            tool_calls=ev.tool_calls or [],
            seq=ev.seq,
            ts=ev.ts,
            attachments=_extract_attachments(ev),
        ))
        state.tokens_out += int(ev.payload.get("completion_tokens", 0) or 0)
        state.tokens_in += int(ev.payload.get("prompt_tokens", 0) or 0)
        state.cost_total += float(ev.payload.get("cost", 0.0) or 0.0)
    elif t == "system_message":
        state.messages.append(Message(
            role="system",
            content=ev.content or "",
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
