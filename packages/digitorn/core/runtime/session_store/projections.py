"""Apply an Event to a SessionState's live projections."""

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
    """Mutate `state`'s live projections to reflect `ev`."""
    t = ev.type

    if t in _NO_PROJECTION_TYPES:
        return

    if t == "user_message":
        msg_text = ev.content or str(ev.payload.get("content", "") or "")
        if ev.kind == "message":
            state.messages.append(Message(
                role="user",
                content=msg_text,
                seq=ev.seq,
                ts=ev.ts,
                attachments=_extract_attachments(ev),
            ))
        if not state.title and msg_text:
            state.title = msg_text.strip()[:80]
        if state.interrupted:
            state.interrupted = False
            state.interrupted_at = None
    elif t == "assistant_message":
        if ev.kind == "message":
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
            _slot = ev.payload.get("agent_seq")
            if isinstance(_slot, int):
                state.streaming_partials.pop(_slot, None)
    elif t == "assistant_message_partial":
        _slot = ev.payload.get("agent_seq")
        _partial = ev.content or str(ev.payload.get("content", "") or "")
        if isinstance(_slot, int):
            state.streaming_partials[_slot] = _partial
    elif t == "turn_terminal":
        state.turn_count += 1
    elif t == "session_title":
        # Explicit title-set event; idempotent.
        new_title = str(ev.payload.get("title", "") or ev.content or "").strip()
        if new_title:
            state.title = new_title[:200]
    elif t == "abort":
        state.interrupted = True
        state.interrupted_at = ev.ts
        state.streaming_partials.clear()
    elif t == "session_workspace":
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
    elif t == "memory_goal_set":
        state.goal = str(ev.payload.get("goal", ""))
    elif t == "memory_task_create":
        todo_id = str(ev.payload.get("id") or "")
        if todo_id:
            state.todos.append(Todo(
                id=todo_id,
                text=str(ev.payload.get("content", "")),
                status=str(ev.payload.get("status", "pending")),
                created_at=ev.ts,
                updated_at=ev.ts,
            ))
    elif t == "memory_task_update":
        todo_id = str(ev.payload.get("id") or "")
        for todo in state.todos:
            if todo.id == todo_id:
                if "status" in ev.payload:
                    todo.status = str(ev.payload["status"])
                if "content" in ev.payload:
                    todo.text = str(ev.payload["content"])
                todo.updated_at = ev.ts
                break
    elif t == "memory_fact_added":
        fact_id = str(ev.payload.get("id") or "")
        if fact_id:
            # Dedup on replay: if the same id already exists, replace
            # in place so a later mutation wins.
            for i, f in enumerate(state.semantic_facts):
                if f.get("id") == fact_id:
                    state.semantic_facts[i] = dict(ev.payload)
                    break
            else:
                state.semantic_facts.append(dict(ev.payload))
    elif t == "memory_fact_forgotten":
        fact_id = str(ev.payload.get("id") or "")
        if fact_id:
            state.semantic_facts = [
                f for f in state.semantic_facts if f.get("id") != fact_id
            ]
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
