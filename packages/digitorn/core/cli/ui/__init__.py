"""CLI UI utilities - labels and tool name formatting.

The visual rendering is handled by the Textual TUI (cli/tui/).
This package only exports label utilities used by the daemon
for event formatting (tool_label, result_status).
"""

from __future__ import annotations

# Labels - used by daemon (manager.py, apps.py) and TUI
from .labels import (
    ACTION_LABELS as ACTION_LABELS,
    MCP_ACTION_LABELS as MCP_ACTION_LABELS,
    TOOL_LABELS as TOOL_LABELS,
    result_status as result_status,
    tool_label as tool_label,
)

# Backward-compatible aliases (used by daemon internals)
_tool_label = tool_label
_result_status = result_status
_TOOL_LABELS = TOOL_LABELS
_MCP_ACTION_LABELS = MCP_ACTION_LABELS
