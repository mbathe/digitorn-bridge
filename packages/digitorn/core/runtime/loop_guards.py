"""Loop guards - detect and break infinite tool-call loops.

The DETECTION logic lives here (counters, thresholds, signature
hashing). The MESSAGES the LLM sees come from
``digitorn.core.runtime.system_directives`` — keep them centralised so
the supervisor voice stays consistent and the wording can be revised
in one place across all enforcement paths.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from digitorn.core.runtime.system_directives import (
    SYS_HINT_DELEGATE,
    SYS_HINT_LARGE_READ,
    SYS_HINT_PARALLEL_READ,
    SYS_HINT_SPAWNED,
    SYS_LOOP_HARD_KILL,
    SYS_LOOP_REPETITION,
    SYS_LOOP_RETRY_DIFFERENT,
    SYS_LOOP_SAME_TOOL,
)

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

    # Hard kill switch: when the soft notes have been ignored for this
    # many failures in a row, agent_loop is told to break the turn
    # rather than keep iterating. The soft-note threshold above
    # (``max_consecutive_failures``) is a hint to the LLM; this one is
    # an enforcement ceiling for the daemon. Was 24 originally -- the
    # ``digitorn-lovable`` zombie ran 1947 retries before the user
    # aborted, 24 capped that. After observing the LLM keeps emitting
    # ``name=""`` even with the soft note in its context, lowered to 12
    # so the kill fires within ~6-8s of stream time. Soft note still
    # has 4 iterations (8 -> 12) to nudge a recoverable failure (wrong
    # path, transient network) before the hard kill ends the turn.
    max_consecutive_failures_hard: int = 12

    # Set to True by ``_check_consecutive_failures`` once the hard cap
    # fires. ``agent_loop`` reads it after each tool call and breaks
    # the turn with a structured ``loop_kill`` result so the LLM stream
    # is finalised cleanly (final ``message_done`` event + DB persist).
    kill_turn_reason: str = ""

    @classmethod
    def from_runtime_config(cls, rt: Any) -> LoopState:
        return cls(
            max_consecutive_failures=getattr(rt, "max_consecutive_failures", 8),
            max_repeat_window=getattr(rt, "max_repeat_window", 20),
            max_repeats=getattr(rt, "max_repeats", 8),
            max_consecutive_same_tool=getattr(rt, "max_consecutive_same_tool", 30),
            max_consecutive_failures_hard=getattr(
                rt, "max_consecutive_failures_hard", 12,
            ),
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

        # Hard ceiling: the soft note below was ignored. The daemon now
        # signals agent_loop to break the turn. We do NOT reset the
        # counter here so a subsequent ``ok=True`` is still required to
        # clear it (see else-branch below) -- prevents oscillation where
        # an intermittent failure flips the kill flag on/off.
        if (
            state.consecutive_failures >= state.max_consecutive_failures_hard
            and not state.kill_turn_reason
        ):
            state.kill_turn_reason = (
                f"loop_guard_hard_kill: tool '{tool_name}' failed "
                f"{state.consecutive_failures} times in a row "
                f"(hard cap = {state.max_consecutive_failures_hard}). "
                f"Turn aborted to prevent runaway."
            )
            logger.error(
                "loop_guard_hard_kill tool=%s consecutive_failures=%d",
                tool_name, state.consecutive_failures,
            )
            return [SYS_LOOP_HARD_KILL.format(
                tool=tool_name,
                n=state.consecutive_failures,
                cap=state.max_consecutive_failures_hard,
            )]

        if state.consecutive_failures >= state.max_consecutive_failures:
            # Soft note: leave the counter so we can keep accumulating
            # toward the hard cap above. Previous behaviour reset it
            # here, which meant the LLM got the SAME note every N
            # failures but no escalation -- exactly the pattern that
            # let digitorn-lovable rack up 1947 retries.
            logger.warning(
                "Retry loop (soft note): %s failed %d times",
                tool_name, state.consecutive_failures,
            )
            return [SYS_LOOP_RETRY_DIFFERENT.format(
                tool=tool_name,
                n=state.consecutive_failures,
                hard_cap=state.max_consecutive_failures_hard,
            )]
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
        return [SYS_LOOP_REPETITION.format(tool=tool_name, n=count)]
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
        return [SYS_LOOP_SAME_TOOL.format(tool=tool_name, n=count)]
    return []


def _check_large_read(
    tool_name: str, ok: bool, result_len: int, consecutive_same: int,
) -> list[str]:
    notes: list[str] = []
    read_tools = ("filesystem.read", "filesystem__read", "read")

    if tool_name in read_tools and ok and result_len > 8000:
        line_count = result_len // 60  # rough estimate
        notes.append(SYS_HINT_LARGE_READ.format(
            lines=line_count, chars=result_len,
        ))

    if tool_name in read_tools and ok and consecutive_same >= 2:
        notes.append(SYS_HINT_PARALLEL_READ)

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
        notes.append(SYS_HINT_SPAWNED.format(n=len(spawned)))
    elif total_tools > 10 and can_spawn:
        notes.append(SYS_HINT_DELEGATE.format(n=total_tools))
    return notes
