"""Event classification helpers for TUI backends.

Pure functions that classify tool names as memory/agent tools
and extract result data from various response formats.
No runtime dependencies — usable by any client.
"""

from __future__ import annotations

from typing import Any


_MEMORY_TOOLS = {
    "set_goal", "remember", "recall", "forget",
    "add_todo", "update_todo",
    # Short PascalCase names (as sent by the LLM via tool_names.py)
    "SetGoal", "Remember", "Recall", "Forget",
    "TodoAdd", "TodoUpdate",
}

_AGENT_TOOLS = {
    "spawn_agent", "agent_wait", "agent_wait_all",
    "agent_result", "agent_status", "agent_cancel", "agent_list",
}


def is_memory_tool(name: str) -> bool:
    """Check if a tool name is a memory-related tool."""
    if "memory" in name:
        return True
    action = name.rsplit(".", 1)[-1].rsplit("__", 1)[-1]
    return action in _MEMORY_TOOLS


def is_agent_tool(name: str) -> bool:
    """Check if name is an agent tool (any format)."""
    flat = name.replace(".", "_").replace("__", "_").lower()
    if any(t in flat for t in (
        "spawn_agent", "agent_wait", "agent_result",
        "agent_spawn", "agent_cancel", "agent_status", "agent_list",
    )):
        return True
    action = name.rsplit(".", 1)[-1].rsplit("__", 1)[-1]
    return action in _AGENT_TOOLS


def extract_result_data(result: Any) -> dict | None:
    """Extract data dict from result (dict or object with .data attribute)."""
    if isinstance(result, dict):
        return result.get("data", result) if "data" in result else result
    if hasattr(result, "data") and isinstance(getattr(result, "data", None), dict):
        return result.data
    return None
