"""Background notifications - formatting and memory persistence."""

from __future__ import annotations

import json
import logging
from typing import Any

from digitorn.core.runtime.types import AgentContext

logger = logging.getLogger(__name__)


_AGENT_EVENT_TYPES = {
    "agent_spawn", "agent_progress", "agent_retrying",
    "agent_completed", "agent_failed", "agent_timeout",
    "agent_cancelled", "agent_cancel",
}


def inject_bg_notifications(ctx: AgentContext, messages: list[dict[str, Any]]) -> None:
    """Drain background task notifications and inject as system messages."""
    cb = ctx.context_builder
    if cb is None:
        return
    drain_fn = getattr(cb, "drain_bg_notifications", None)
    if drain_fn is None:
        return
    notifications = drain_fn(session_id=ctx.session_id)
    if not isinstance(notifications, list):
        return

    for notif in notifications:
        ntype = notif.get("type", "")
        if ntype == "watcher":
            text = format_watcher_notification(notif)
        elif ntype in _AGENT_EVENT_TYPES:
            text = format_agent_notification(notif)
        else:
            text = format_bg_task_notification(notif)
        messages.append({"role": "system", "content": text})
        _persist_to_memory(ctx, notif)

    if notifications:
        logger.info("Injected %d background notification(s)", len(notifications))


def format_bg_task_notification(notif: dict[str, Any]) -> str:
    """Format a background task notification as text for the LLM."""
    task_id = notif.get("task_id", "?")
    tool_name = notif.get("tool_name", "?")
    status = notif.get("status", "unknown")
    elapsed = notif.get("elapsed_seconds", 0)

    # Progress notification (intermediate update, task still running)
    if status == "progress":
        preview = notif.get("result_preview", "")
        hint = notif.get("hint", "")
        return (
            f"[BACKGROUND TASK PROGRESS] task_id={task_id}, "
            f"tool={tool_name}, elapsed={elapsed}s\n{preview}\n{hint}"
        )

    if status == "cancelled":
        hint = notif.get("hint", "")
        return (
            f"[BACKGROUND TASK CANCELLED] task_id={task_id}, "
            f"tool={tool_name}, elapsed={elapsed}s\n{hint}"
        )

    if status == "failed":
        error = notif.get("error", "Unknown error")
        return (
            f"[BACKGROUND TASK FAILED] task_id={task_id}, "
            f"tool={tool_name}, elapsed={elapsed}s\nError: {error}"
        )

    if "result_preview" in notif:
        hint = notif.get("hint", "")
        return (
            f"[BACKGROUND TASK COMPLETED] task_id={task_id}, "
            f"tool={tool_name}, elapsed={elapsed}s\n"
            f"Result (truncated): {notif['result_preview']}\n{hint}"
        )

    result_data = notif.get("result")
    try:
        result_str = json.dumps(result_data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        result_str = str(result_data)
    return (
        f"[BACKGROUND TASK COMPLETED] task_id={task_id}, "
        f"tool={tool_name}, elapsed={elapsed}s\nResult: {result_str}"
    )


def format_agent_notification(notif: dict[str, Any]) -> str:
    """Format a sub-agent lifecycle event for the coordinator's prompt."""
    ev_type = notif.get("type", "")
    agent_id = notif.get("agent_id", "?")
    specialist = notif.get("specialist") or "generic"
    task = (notif.get("task") or "")[:200]
    duration = notif.get("duration_seconds")
    duration_s = f"{duration:.1f}s" if isinstance(duration, (int, float)) else "?"

    # Terminal events (one of: completed, failed, timeout, cancelled).
    if ev_type == "agent_completed":
        result_summary = (notif.get("result_summary")
                          or notif.get("preview") or "")[:300]
        tool_calls_count = notif.get("tool_calls_count", 0)
        turns = notif.get("turns", 0)
        body = (
            f"[SUB-AGENT COMPLETED] agent_id={agent_id}, specialist={specialist}\n"
            f"Task: {task}\n"
            f"Duration: {duration_s} | Turns: {turns} | Tool calls: {tool_calls_count}\n"
            f"Result: {result_summary}"
        )
        return body

    if ev_type == "agent_failed":
        error = notif.get("error") or "(no diagnostic - check daemon logs)"
        turns = notif.get("turns", 0)
        return (
            f"[SUB-AGENT FAILED] agent_id={agent_id}, specialist={specialist}\n"
            f"Task: {task}\n"
            f"Duration: {duration_s} | Turns: {turns}\n"
            f"Error: {str(error)[:400]}"
        )

    if ev_type == "agent_timeout":
        return (
            f"[SUB-AGENT TIMEOUT] agent_id={agent_id}, specialist={specialist}\n"
            f"Task: {task}\n"
            f"Duration: {duration_s} (timed out before completing). "
            f"Consider raising `timeout` or breaking the task into smaller chunks."
        )

    if ev_type in ("agent_cancelled", "agent_cancel"):
        reason = notif.get("reason") or notif.get("error") or "no reason given"
        return (
            f"[SUB-AGENT CANCELLED] agent_id={agent_id}, specialist={specialist}\n"
            f"Task: {task}\n"
            f"Duration: {duration_s} | Reason: {str(reason)[:200]}"
        )

    if ev_type == "agent_retrying":
        attempt = notif.get("attempt", "?")
        max_attempts = notif.get("max_attempts", "?")
        retry_reason = notif.get("reason", "?")
        return (
            f"[SUB-AGENT RETRYING] agent_id={agent_id}, specialist={specialist}\n"
            f"Task: {task}\n"
            f"Attempt {attempt}/{max_attempts} (reason: {retry_reason})"
        )

    if ev_type == "agent_progress":
        turns = notif.get("turns", 0)
        tool_calls = notif.get("tool_calls_count", 0)
        preview = (notif.get("preview") or "")[:200]
        if not preview and tool_calls == 0 and turns == 0:
            return ""
        return (
            f"[SUB-AGENT PROGRESS] agent_id={agent_id}, specialist={specialist}\n"
            f"Turns: {turns} | Tool calls: {tool_calls}\n"
            f"Latest: {preview}"
        )

    if ev_type == "agent_spawn":
        return (
            f"[SUB-AGENT SPAWNED] agent_id={agent_id}, specialist={specialist}\n"
            f"Task: {task}"
        )

    # Unknown agent_* event - degrade to generic but with proper
    # field names so the LLM can still parse what happened.
    return (
        f"[SUB-AGENT EVENT {ev_type}] agent_id={agent_id}, "
        f"specialist={specialist}, status={notif.get('status', '?')}"
    )


def format_watcher_notification(notif: dict[str, Any]) -> str:
    """Format a watcher notification as text for the LLM."""
    watcher_id = notif.get("watcher_id", "?")
    label = notif.get("label", "")
    tool_name = notif.get("tool_name", "?")
    check_num = notif.get("check_number", 0)
    notify_count = notif.get("notify_count", 0)
    interval = notif.get("interval", 0)
    strategy = notif.get("strategy", "?")

    header = f"[WATCHER UPDATE] watcher_id={watcher_id}"
    if label:
        header += f', label="{label}"'
    header += f", tool={tool_name}"
    header += f"\nCheck #{check_num} (interval: {interval}s, "
    header += f"{notify_count} notification(s) so far, strategy: {strategy})"

    error = notif.get("error")
    if error:
        return f"{header}\nError: {error}"

    if "summary_batch" in notif:
        batch = notif["summary_batch"]
        try:
            batch_str = json.dumps(batch, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            batch_str = str(batch)
        return f"{header}\nSummary ({len(batch)} checks): {batch_str}"

    if "result_preview" in notif:
        return f"{header}\nResult (truncated): {notif['result_preview']}"

    result_data = notif.get("result")
    try:
        result_str = json.dumps(result_data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        result_str = str(result_data)
    return f"{header}\nResult: {result_str}"


_MAX_FACTS = 15

_UI_ONLY_NOTIF_TYPES = frozenset({
    "fact_added",       # memory.remember -- data already in store.semantic.facts
    "goal_set",         # memory.set_goal -- data already in store.working.goal
    "todo_added",       # memory.task_create -- data already in store.working.todos
    "todo_updated",     # memory.task_update -- data already in store.working.todos
    "task_created",     # alias of todo_added (legacy)
    "task_updated",     # alias of todo_updated (legacy)
})


def _persist_to_memory(ctx: AgentContext, notif: dict[str, Any]) -> None:
    mem = ctx.memory_module
    if mem is None:
        return

    store = mem.store
    notif_type = notif.get("type", "")

    if notif_type in _UI_ONLY_NOTIF_TYPES:
        return

    if notif_type == "watcher":
        _persist_watcher(store, notif)
    elif notif_type == "scheduled_job":
        _persist_scheduled_job(store, notif)
    elif notif_type.startswith("agent_"):
        _persist_agent_event(store, notif)
    else:
        _persist_bg_task(store, notif)

    if len(store.working.key_facts) > _MAX_FACTS:
        store.working.key_facts = store.working.key_facts[-_MAX_FACTS:]


def _persist_watcher(store: Any, notif: dict[str, Any]) -> None:
    label = notif.get("label", "watcher")
    check = notif.get("check_number", "?")
    error = notif.get("error", "")

    if error:
        fact = f"[Watcher: {label}] Check #{check} ERROR: {str(error)[:100]}"
    else:
        result = notif.get("result", "")
        preview = str(result)[:100] if result else "ok"
        fact = f"[Watcher: {label}] Check #{check}: {preview}"

    store.working.key_facts.append(fact)
    logger.debug("memory_watcher_fact: %s", fact[:80])


def _persist_scheduled_job(store: Any, notif: dict[str, Any]) -> None:
    label = notif.get("label", "")
    memory_context = notif.get("memory_context", "")
    prompt = notif.get("prompt", "")
    result = notif.get("result", "")

    if memory_context or (label and label.startswith("Remember:")):
        reminder = memory_context or prompt or label
        store.working.add_todo(f"REMINDER: {reminder}")
        logger.debug("memory_reminder_todo: %s", reminder[:80])

    if result:
        fact = f"[Job: {label}] Result: {str(result)[:100]}"
        store.working.key_facts.append(fact)


def _persist_agent_event(store: Any, notif: dict[str, Any]) -> None:
    agent_id = notif.get("agent_id", "")
    task = notif.get("task", "")
    status = notif.get("status", "")
    specialist = notif.get("specialist", "")
    content_preview = notif.get("content_preview", "")
    facts_count = notif.get("facts_count", 0)
    errors = notif.get("errors", [])
    duration = notif.get("duration_seconds", 0)

    spec_tag = f" ({specialist})" if specialist else ""
    if status == "completed":
        fact = (
            f"[Agent {agent_id}{spec_tag}] Completed in {duration}s: "
            f"{task[:80]}. {facts_count} findings."
        )
        if content_preview:
            fact += f" Result: {content_preview[:100]}"
    else:
        error_msg = errors[0] if errors else "unknown error"
        fact = f"[Agent {agent_id}{spec_tag}] FAILED: {task[:60]}. Error: {error_msg[:80]}"

    store.working.key_facts.append(fact)
    logger.debug("memory_agent_fact: %s", fact[:80])


def _persist_bg_task(store: Any, notif: dict[str, Any]) -> None:
    tool_name = notif.get("tool_name", "")
    status = notif.get("status", "")
    result = notif.get("result", "")
    error = notif.get("error", "")
    elapsed = notif.get("elapsed_seconds", "")

    if error:
        fact = f"[Background: {tool_name}] FAILED: {str(error)[:100]}"
    elif result:
        fact = f"[Background: {tool_name}] Completed ({elapsed}s): {str(result)[:150]}"
    else:
        fact = f"[Background: {tool_name}] {status}"

    store.working.key_facts.append(fact)
    logger.debug("memory_bg_fact: %s", fact[:80])
