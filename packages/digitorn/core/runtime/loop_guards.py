"""Loop guards — detect and break infinite tool-call loops."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_INCREMENTAL_ACTIONS = frozenset({
    "append_rows", "spreadsheet__append_rows",
    "write_sheet", "spreadsheet__write_sheet",
    "execute_query", "database__execute_query",
    "add_slide", "presentation__add_slide",
})

_RESEARCH_ACTIONS = frozenset({
    # Web
    "search", "web__search", "web_search",
    "fetch", "web__fetch", "web_fetch",
    "fetch_page", "http__fetch_page",
    "get", "http__get",
    # Filesystem (read-heavy workflows are normal)
    "read", "filesystem__read", "read_file", "filesystem__read_file",
    "find", "filesystem__find",
    "grep", "filesystem__grep",
    "glob", "filesystem__glob",
    "write", "filesystem__write",
    "edit", "filesystem__edit",
    # Shell (git workflows = many consecutive bash calls)
    "bash", "shell__bash",
    # Memory
    "task_create", "memory__task_create",
    "task_update", "memory__task_update",
    "remember", "memory__remember",
    "set_goal", "memory__set_goal",
    # Agent (parallel spawns = multiple agent calls)
    "agent", "agent_spawn__agent",
    # Discovery
    "run_parallel",
})


@dataclass
class LoopState:
    """Mutable state for loop detection across the entire agent turn."""

    counter: dict[str, int] = field(default_factory=lambda: {"tools": 0, "turns": 0})

    last_failed_tool: str = ""
    consecutive_failures: int = 0
    max_consecutive_failures: int = 8

    recent_calls: list[str] = field(default_factory=list)
    max_repeat_window: int = 20
    max_repeats: int = 8

    last_tool_name: str = ""
    consecutive_same_tool: int = 0
    max_consecutive_same_tool: int = 30

    @classmethod
    def from_runtime_config(cls, rt: Any) -> LoopState:
        return cls(
            max_consecutive_failures=getattr(rt, "max_consecutive_failures", 8),
            max_repeat_window=getattr(rt, "max_repeat_window", 20),
            max_repeats=getattr(rt, "max_repeats", 8),
            max_consecutive_same_tool=getattr(rt, "max_consecutive_same_tool", 30),
        )


def check_tool_health(
    state: LoopState,
    tool_name: str,
    tool_args: dict[str, Any],
    ok: bool,
    result_len: int,
) -> list[str]:
    """Run all loop-detection checks after a tool call.

    Returns a list of deferred system notes to inject.
    """
    notes: list[str] = []
    notes.extend(_check_consecutive_failures(state, tool_name, ok))
    notes.extend(_check_repetition(state, tool_name, tool_args))
    notes.extend(_check_same_tool_loop(state, tool_name))
    notes.extend(_check_large_read(tool_name, ok, result_len, state.consecutive_same_tool))
    return notes


def _check_consecutive_failures(
    state: LoopState, tool_name: str, ok: bool,
) -> list[str]:
    if not ok:
        if tool_name == state.last_failed_tool:
            state.consecutive_failures += 1
        else:
            state.last_failed_tool = tool_name
            state.consecutive_failures = 1

        if state.consecutive_failures >= state.max_consecutive_failures:
            state.consecutive_failures = 0
            state.last_failed_tool = ""
            logger.warning("Retry loop: %s failed %d+ times", tool_name, state.max_consecutive_failures)
            return [
                f"The tool '{tool_name}' has failed "
                f"{state.max_consecutive_failures} times in a row. "
                "Do NOT retry the same approach. Instead:\n"
                "1. Try a DIFFERENT tool or method to achieve the same goal.\n"
                "2. If the tool requires specific parameters, check the schema with get_tool.\n"
                "3. If it's a permission issue, try an alternative action that is allowed.\n"
                "4. If you're stuck, delegate the task to a sub-agent or ask the user."
            ]
    else:
        state.consecutive_failures = 0
        state.last_failed_tool = ""

    return []


def _check_repetition(
    state: LoopState, tool_name: str, tool_args: dict[str, Any],
) -> list[str]:
    sig_input = str(sorted(tool_args.items()) if isinstance(tool_args, dict) else str(tool_args))
    sig = f"{tool_name}:{hashlib.md5(sig_input.encode()).hexdigest()[:8]}"
    state.recent_calls.append(sig)

    if len(state.recent_calls) > state.max_repeat_window:
        state.recent_calls.pop(0)

    count = state.recent_calls.count(sig)
    if count >= state.max_repeats:
        state.recent_calls.clear()
        logger.warning("Repetition loop: %s called %d times", tool_name, count)
        return [
            f"You already called '{tool_name}' with the same parameters "
            f"{count} times and got the same result each time. "
            "The data is already in your conversation. Use it directly. "
            "If you need different data, change the parameters or use a different tool."
        ]
    return []


def _check_same_tool_loop(state: LoopState, tool_name: str) -> list[str]:
    if tool_name == state.last_tool_name:
        state.consecutive_same_tool += 1
    else:
        state.last_tool_name = tool_name
        state.consecutive_same_tool = 1

    base = tool_name.rsplit(".", 1)[-1] if "." in tool_name else tool_name
    is_exempt = (
        tool_name in _INCREMENTAL_ACTIONS
        or base in _INCREMENTAL_ACTIONS
        or tool_name in _RESEARCH_ACTIONS
        or base in _RESEARCH_ACTIONS
    )

    if state.consecutive_same_tool >= state.max_consecutive_same_tool and not is_exempt:
        logger.warning("Same-tool loop: %s called %d times", tool_name, state.consecutive_same_tool)
        count = state.consecutive_same_tool
        state.consecutive_same_tool = 0
        return [
            f"You have called '{tool_name}' {count} "
            "times in a row. Step back and reconsider your approach:\n"
            "- If it keeps failing, the tool or parameters are wrong. Try something different.\n"
            "- If you have the data you need, stop calling and use it.\n"
            "- If the task is too complex, delegate to a sub-agent.\n"
            "- If you're stuck, tell the user what's blocking you."
        ]
    return []


def _check_large_read(
    tool_name: str, ok: bool, result_len: int, consecutive_same: int,
) -> list[str]:
    notes: list[str] = []
    read_tools = ("filesystem.read", "filesystem__read", "read")

    if tool_name in read_tools and ok and result_len > 8000:
        line_count = result_len // 60  # rough estimate
        notes.append(
            f"Note: The file you just read is large (~{line_count} lines, "
            f"{result_len} chars). To protect your context window:\n"
            "- Use start_line/end_line to read specific sections next time.\n"
            "- Use grep to find specific patterns instead of reading the whole file.\n"
            "- Delegate large file analysis to a sub-agent if one is available."
        )

    if tool_name in read_tools and ok and consecutive_same >= 2:
        notes.append(
            "Tip: You are reading multiple files sequentially. "
            "Use run_parallel to read them all at once -- it's faster."
        )

    return notes


def check_delegation(
    tool_calls: list[dict[str, Any]],
    total_tools: int,
    available_tools: list[dict[str, Any]],
) -> list[str]:
    """Check if spawned agents need notification or delegation hints."""
    notes: list[str] = []
    spawned = [c for c in tool_calls if "spawn" in c.get("function", {}).get("name", "")]
    can_spawn = any("spawn_agent" in t.get("function", {}).get("name", "") for t in available_tools)

    if spawned:
        notes.append(
            f"You just spawned {len(spawned)} sub-agent(s). "
            "They are running in the background. You will be automatically notified "
            "when they complete — no need to poll. Continue with other work in the meantime."
        )
    elif total_tools > 10 and can_spawn:
        notes.append(
            "DELEGATION REMINDER: You have made many tool calls. "
            "Consider delegating the next heavy task to a sub-agent to protect your context. "
            "Use spawn_agent(specialist='explore', task='...') for codebase exploration, "
            "or spawn_agent(specialist='worker', task='...') for independent subtasks."
        )
    return notes
