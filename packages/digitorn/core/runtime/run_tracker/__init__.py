"""Public API for agent-run tracking."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from digitorn.core.runtime.run_tracker.protocols import TrackerBackend
from digitorn.core.runtime.run_tracker.worker import (
    allocate_sequence,
    enqueue,
    forget_run,
    install_and_start,
    is_running,
    stop,
)

logger = logging.getLogger(__name__)


__all__ = [
    "TrackerBackend",
    "install_and_start",
    "stop",
    "is_running",
    "start_run",
    "complete_run",
    "emit_event",
    "increment_turns",
    "increment_sub_agents_spawned",
    "new_run_id",
]


def new_run_id() -> str:
    """Generate a fresh agent-run id without any I/O."""
    return uuid.uuid4().hex


def start_run(
    ctx: Any,
    max_turns: int | None,
    *,
    parent_run_id: Optional[str] = None,
    task_summary: Optional[str] = None,
) -> str:
    """Open a new run"""
    run_id = new_run_id()

    user_id = (getattr(ctx, "user_id", None) or "").strip()
    app_id = getattr(ctx, "app_id", None) or ""
    session_id = getattr(ctx, "session_id", None) or ""
    if not user_id or not app_id or not session_id:
        return run_id

    provider_obj = getattr(ctx, "provider", None)
    ctx_snapshot = {
        "user_id": user_id,
        "app_id": app_id,
        "session_id": session_id,
        "agent_id": getattr(ctx, "agent_id", None) or "default",
        "agent_name": getattr(ctx, "agent_name", None),
        "workspace": getattr(ctx, "workspace", "") or "",
        "provider": (
            getattr(provider_obj, "provider_hint", None)
            or getattr(provider_obj, "provider_id", None)
        ) if provider_obj is not None else None,
        "model": getattr(provider_obj, "model", None) if provider_obj is not None else None,
    }

    now_iso = datetime.now(timezone.utc).isoformat()
    enqueue("start", {
        "run_id": run_id,
        "ctx_snapshot": ctx_snapshot,
        "max_turns": max_turns,
        "parent_run_id": parent_run_id,
        "task_summary": task_summary,
        "queued_at_iso": now_iso,
        "started_at_iso": now_iso,
    })
    return run_id


def complete_run(
    run_id: Optional[str],
    *,
    status: str,
    turn_result: Any | None = None,
    status_reason: Optional[str] = None,
) -> None:
    """Close a run. Tokens / turns are pulled from `turn_result` if"""
    if not run_id:
        return

    prompt_tokens = 0
    completion_tokens = 0
    turns_used = 0
    if turn_result is not None:
        prompt_tokens = int(getattr(turn_result, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(turn_result, "completion_tokens", 0) or 0)
        turns_used = int(getattr(turn_result, "turns_used", 0) or 0)

    enqueue("complete", {
        "run_id": run_id,
        "status": status,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "turns_used": turns_used,
        "status_reason": status_reason,
        "completed_at_iso": datetime.now(timezone.utc).isoformat(),
    })


def emit_event(
    run_id: Optional[str],
    event_type: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Append one row to agent_run_events. Sequence is allocated"""
    if not run_id:
        return
    sequence = allocate_sequence(run_id)
    enqueue("event", {
        "run_id": run_id,
        "event_type": event_type,
        "data": dict(data or {}),
        "sequence": sequence,
        "emitted_at_iso": datetime.now(timezone.utc).isoformat(),
    })


def increment_turns(run_id: Optional[str]) -> None:
    if not run_id:
        return
    enqueue("inc_turns", {"run_id": run_id})


def increment_sub_agents_spawned(run_id: Optional[str]) -> None:
    if not run_id:
        return
    enqueue("inc_subs", {"run_id": run_id})
