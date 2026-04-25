"""Tool-level hooks — pre/post tool execution events.

Extends the hook system with ``tool_start`` and ``tool_end`` events.
These fire around individual tool calls (not turns), enabling patterns like:
  - Run a linter after every filesystem.edit
  - Log every shell.run invocation
  - Validate params before a dangerous tool executes

Condition: ``tool_match``
    Fires when the tool name matches one of the specified patterns.

Usage in YAML::

    hooks:
      - id: lint_after_edit
        on: tool_end
        condition:
          type: tool_match
          tools: ["filesystem.edit", "filesystem.write"]
        action:
          type: module_action
          module: shell
          action: run
          params:
            command: "ruff check {{tool.path}}"
        cooldown: 5
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from digitorn.core.runtime.hooks import register_condition, TurnState

logger = logging.getLogger(__name__)


@dataclass
class ToolCallContext:
    """Context for a tool-level hook evaluation.

    Attached to ``TurnState.tool_context`` so conditions and actions
    can inspect the current tool call.
    """

    tool_name: str = ""
    tool_params: dict[str, Any] = field(default_factory=dict)
    tool_result: Any = None
    tool_ok: bool = True
    tool_elapsed: float = 0.0


@register_condition("tool_match")
def _eval_tool_match(state: TurnState, params: dict[str, Any]) -> bool:
    """Fire when the current tool matches one of the listed names.

    Params:
        tools: list of tool names (exact match or prefix with ``*``).
            Examples: ``["filesystem.edit"]``, ``["filesystem.*"]``, ``["shell.run"]``

    The tool name is read from ``state.tool_context.tool_name``.
    """
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
    """Create a TurnState copy with tool_context attached for hook evaluation.

    Does NOT copy messages (they share the same list reference — this is intentional
    so that hook actions like inject_message affect the live conversation).
    """
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
