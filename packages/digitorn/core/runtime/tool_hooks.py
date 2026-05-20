"""Tool-level hooks - pre/post tool execution events."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from digitorn.core.runtime.hooks import register_condition, TurnState

logger = logging.getLogger(__name__)


@dataclass
class ToolCallContext:
    """Context for a tool-level hook evaluation."""

    tool_name: str = ""
    tool_params: dict[str, Any] = field(default_factory=dict)
    tool_result: Any = None
    tool_ok: bool = True
    tool_elapsed: float = 0.0


@register_condition("tool_match")
def _eval_tool_match(state: TurnState, params: dict[str, Any]) -> bool:
    """Fire when the current tool matches one of the listed names."""
    tool_ctx: ToolCallContext | None = getattr(state, "tool_context", None)
    if tool_ctx is None:
        return False

    tool_name = tool_ctx.tool_name
    patterns: list[str] = params.get("tools", [])

    for pattern in patterns:
        if pattern.endswith(".*"):
            prefix = pattern[:-1]  # "filesystem." from "filesystem.*"
            if tool_name.startswith(prefix):
                return True
        elif tool_name == pattern:
            return True

    return False


def make_tool_state(
    base_state: TurnState,
    tool_name: str,
    tool_params: dict[str, Any],
    *,
    result: Any = None,
    ok: bool = True,
    elapsed: float = 0.0,
) -> TurnState:
    """Create a TurnState copy with tool_context attached for hook evaluation."""
    # Shallow copy via dataclass replace isn't available for non-frozen dataclasses,
    # so we just attach the extra attribute.
    state = base_state
    state.tool_context = ToolCallContext(  # type: ignore[attr-defined]
        tool_name=tool_name,
        tool_params=tool_params,
        tool_result=result,
        tool_ok=ok,
        tool_elapsed=elapsed,
    )
    return state
