"""Memory Hooks - automatic memory management during the agent loop."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

def build_memory_prompt_section(memory_module: Any) -> str:
    """Build the memory section for the system prompt."""
    if memory_module is None:
        return ""

    store = memory_module.store
    config = memory_module.memory_config

    if not config.has_any_layer():
        return ""

    return store.render_full_snapshot()

def build_memory_instructions(
    memory_module: Any,
    tool_injection: str = "discovery",
) -> str:
    """Build memory usage instructions for the system prompt."""
    if memory_module is None:
        return ""

    config = memory_module.memory_config
    if not config.has_any_layer():
        return ""

    def _call(action: str, example: str = "") -> str:
        ex = f'({example})' if example else "()"
        return f"`{action}{ex}`"

    parts: list[str] = [
        "\n## Memory & Resume Protocol",
        "Your memory is shown above under MEMORY. It survives context",
        "compaction AND session interruption - your tasks ARE your resume",
        "protocol. If a turn crashes (network blip, daemon restart, abort,",
        "context overflow), the runtime re-injects your tasks and asks you",
        "to continue from your `in_progress` entry. The cost of not",
        "checkpointing your intent in tasks is: on interrupt, the next turn",
        "has no idea what you were doing.",
        "",
        "4 memory tools:",
        '  set_goal(goal="...") - set the session objective',
        '  remember(content="...") - store a fact (survives compaction + workers)',
        '  TaskCreate(subject="...", description="...") - add a resume checkpoint',
        '  TaskUpdate(taskId="t1", status="in_progress|completed|blocked") - move the checkpoint',
        "",
        "Rules:",
        "- Simple question / single-step work → just answer or just act,",
        "  no memory tools needed. Tasks come in batches (≥ 2) or not at",
        "  all - a lone task is chrome noise (UI hides it) AND useless for",
        "  resume (1 task = no plan to recover).",
        "- Multi-step task → set_goal first, then TaskCreate for EACH step",
        "  BEFORE acting, then TaskUpdate as you progress. A plan that lives",
        "  only in your head dies with the process.",
        "- Status honesty is non-negotiable. `in_progress` = I am here now.",
        "  `completed` = done AND verified. Lying breaks resume.",
        "- One task `in_progress` at a time. Switching focus → reset the",
        "  previous one to `pending` or `blocked` first.",
        "- Store durable findings with remember() - survives compaction and",
        "  is visible to parallel workers on the same session.",
        "- Your memory is auto-injected every turn - just read it, no query",
        "  tool needed.",
    ]

    return "\n".join(parts)

def on_turn_start(
    memory_module: Any,
    messages: list[dict[str, Any]],
    turn: int,
    session_id: str | None = None,
) -> None:
    """Called at the start of each agent turn."""
    if memory_module is None:
        return

    if session_id is not None:
        memory_module.set_active_session(session_id)

    config = memory_module.memory_config
    store = memory_module.store

    if turn == 0 and store.working.goal:
        progress = store.working.get_progress()
        _inject_system_note(
            messages,
            f"📌 SESSION RESUMED - You were working on: '{store.working.goal}'. "
            f"Progress: {progress['done']}/{progress['total']} tasks done "
            f"({progress['percent']}%). "
            f"Check your memory above and continue where you left off.",
        )
        logger.info(
            "memory_session_resumed session=%s goal=%s progress=%d%%",
            session_id, store.working.goal, progress["percent"],
        )

    if turn == 0 and not store.working.original_request:
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                text = ""
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text") or ""
                        if text:
                            break
                content = text
            if isinstance(content, str) and content.strip():
                store.working.original_request = content.strip()
                break

    if config.has_any_layer() and messages:
        _update_memory_in_messages(store, config, messages)

    if config.runtime_goal_guardian and turn > 0 and not store.working.goal:
        _inject_system_note(
            messages,
            "💡 You haven't set a goal yet. Use `set_goal` to define your objective "
            "so you can track progress effectively.",
        )

def on_tool_result(
    memory_module: Any,
    tool_name: str,
    params: dict[str, Any],
    result: Any,
) -> None:
    """Called after a tool execution completes."""
    if memory_module is None:
        return

    config = memory_module.memory_config
    store = memory_module.store

    if config.runtime_content_cache and "read" in tool_name:
        _auto_cache_file_content(store, tool_name, params, result)

    if config.working_memory:
        _auto_track_entity(store, tool_name, params, result)

def on_turn_end(
    memory_module: Any,
    messages: list[dict[str, Any]],
    turn: int,
    tool_calls_this_turn: list[dict[str, Any]],
) -> None:
    """Called at the end of each agent turn."""
    if memory_module is None:
        return

    config = memory_module.memory_config
    store = memory_module.store

    if config.working_memory and store.working.plan:
        _auto_advance_plan(store, tool_calls_this_turn)

    if config.runtime_goal_guardian and store.working.goal and turn > 2:
        _check_goal_drift(store, messages, turn)

def on_compaction(
    memory_module: Any,
    messages: list[dict[str, Any]],
) -> str:
    """Called during context compaction."""
    if memory_module is None:
        return ""

    config = memory_module.memory_config
    store = memory_module.store

    if not config.has_any_layer():
        return ""

    snapshot = store.render_full_snapshot()
    tool_injection = getattr(memory_module, "_tool_injection", "discovery")
    instructions = build_memory_instructions(memory_module, tool_injection=tool_injection)

    return f"{snapshot}\n{instructions}"

_MEMORY_MARKER_START = "═══ MEMORY ═══"
_MEMORY_MARKER_END = "═══════════════"

def _update_memory_in_messages(
    store: Any, config: Any, messages: list[dict[str, Any]],
) -> None:
    if not messages or messages[0].get("role") != "system":
        return

    system_content = messages[0]["content"]
    snapshot = store.render_full_snapshot()

    if _MEMORY_MARKER_START in system_content:
        pattern = re.escape(_MEMORY_MARKER_START) + r".*?" + re.escape(_MEMORY_MARKER_END)
        new_content = re.sub(pattern, snapshot.replace("\\", "\\\\"), system_content, flags=re.DOTALL)
        messages[0]["content"] = new_content
    else:
        messages[0]["content"] = snapshot + "\n\n" + system_content

def _inject_system_note(messages: list[dict[str, Any]], note: str) -> None:
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] += f"\n\n{note}"
    else:
        messages.insert(0, {"role": "system", "content": note})

def _auto_cache_file_content(
    store: Any, tool_name: str, params: dict[str, Any], result: Any,
) -> None:
    from digitorn.modules.memory.store import CachedContent

    path = params.get("path") or params.get("file_path", "")
    if not path:
        return

    content = ""
    if hasattr(result, "data") and isinstance(result.data, dict):
        content = result.data.get("content", "") or result.data.get("output", "")
    elif isinstance(result, dict):
        content = result.get("content", "") or result.get("output", "")
    elif isinstance(result, str):
        content = result

    if not content or len(content) < 10:
        return

    cached = CachedContent(
        path=path,
        content=content,
        content_hash=CachedContent.compute_hash(content),
        cached_at=time.monotonic(),
        size=len(content),
        summary=f"{len(content)} chars, {content.count(chr(10))+1} lines",
    )
    store.working.content_cache[path] = cached
    store.working.active_entities[path] = cached.summary
    logger.debug("memory_auto_cached path=%s size=%d", path, len(content))

def _auto_track_entity(
    store: Any, tool_name: str, params: dict[str, Any], result: Any,
) -> None:
    path = params.get("path") or params.get("file_path")
    if path and path not in store.working.active_entities:
        store.working.active_entities[path] = f"accessed via {tool_name}"

    directory = params.get("directory") or params.get("dir")
    if directory and directory not in store.working.active_entities:
        store.working.active_entities[directory] = f"explored via {tool_name}"

def _auto_advance_plan(
    store: Any, tool_calls: list[dict[str, Any]],
) -> None:
    if not tool_calls:
        return

    plan = store.working.plan
    step = store.working.current_step

    if step < len(plan):
        step_text = plan[step].lower()
        for call in tool_calls:
            tool = call.get("name", "").lower()
            if any(word in step_text for word in tool.split("_") if len(word) > 2):
                logger.debug(
                    "memory_plan_step_match step=%d tool=%s",
                    step, call.get("name"),
                )
                break

def _check_goal_drift(
    store: Any, messages: list[dict[str, Any]], turn: int,
) -> None:
    goal = store.working.goal.lower()
    if not goal:
        return

    goal_words = set(goal.split())
    goal_words.discard("")

    recent = messages[-6:] if len(messages) > 6 else messages
    found_relevant = False
    for msg in recent:
        content = (msg.get("content") or "").lower()
        overlap = sum(1 for w in goal_words if w in content and len(w) > 3)
        if overlap >= 2:
            found_relevant = True
            break

    if not found_relevant:
        _inject_system_note(
            messages,
            f"🎯 Reminder: your current goal is '{store.working.goal}'. "
            f"Stay focused or update your goal if priorities changed.",
        )
        logger.debug("memory_goal_drift_reminder turn=%d goal=%s", turn, store.working.goal)
