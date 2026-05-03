"""Centralized tool name resolution.

All tool name resolution goes through this module. It handles:
1. Short API names (Write → filesystem.write) - like Claude Code
2. FQN names (filesystem.write) - internal format
3. Double-underscore names (filesystem__write) - OpenAI API format
4. Action-only names (write) - fuzzy match to FQN

This is the SINGLE SOURCE OF TRUTH for tool name mapping.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Short API names → FQN (module.action)
# These match Claude Code's tool names so the model performs optimally.
_SHORT_TO_FQN: dict[str, str] = {
    # Filesystem (6 tools exposed to LLM)
    "Read": "filesystem.read",
    "Write": "filesystem.write",
    "Edit": "filesystem.edit",
    "Grep": "filesystem.grep",
    "Glob": "filesystem.glob",
    "Ls": "filesystem.ls",
    # Shell (1 tool, 5 modes via params)
    "Bash": "shell.bash",
    # Memory (3 tools exposed to LLM - Claude Code compatible)
    "Remember": "memory.remember",
    "TaskCreate": "memory.task_create",
    "TaskUpdate": "memory.task_update",
    # Avoid collision with context_builder.remember (scheduler reminder with what/when)
    "SetReminder": "context_builder.remember",
    # Web (2 tools exposed to LLM)
    "WebSearch": "web.search",
    "WebFetch": "web.fetch",
    # Agent (1 tool, 8 modes via params)
    "Agent": "agent_spawn.agent",
    # Context builder
    "AskUser": "context_builder.ask_user",
    "BackgroundRun": "context_builder.background_run",
    # Workspace (virtual filesystem for live preview apps)
    "WsWrite": "workspace.write",
    "WsRead": "workspace.read",
    "WsEdit": "workspace.edit",
    "WsGlob": "workspace.glob",
    "WsGrep": "workspace.grep",
    "WsDelete": "workspace.delete",
    # Web preview (session-scoped iframe attachments)
    "PreviewProxy": "web_preview.proxy",
    "PreviewStatic": "web_preview.static",
    "PreviewDetach": "web_preview.detach",
    "PreviewList": "web_preview.list",
    # LSP
    "LintCheck": "lsp.diagnostics",
    "LintFile": "lsp.check",
    # Notebook
    "NotebookRead": "notebook.read",
    "NotebookEdit": "notebook.edit_cell",
    "NotebookAdd": "notebook.add_cell",
    "NotebookDelete": "notebook.delete_cell",
    # Database (10 actions exposed to LLM)
    "DbConnect": "database.connect",
    "DbDisconnect": "database.disconnect",
    "DbList": "database.list_connections",
    "DbQuery": "database.sql",
    "DbTransaction": "database.transaction",
    "DbBulkInsert": "database.bulk_insert",
    "DbSchema": "database.schema",
    "DbBrowse": "database.browse",
    "DbRelations": "database.relations",
    "DbSearch": "database.search_data",
}

# Reverse: FQN → short name
_FQN_TO_SHORT: dict[str, str] = {v: k for k, v in _SHORT_TO_FQN.items()}


def to_fqn(name: str) -> str:
    """Resolve ANY tool name format to its FQN (module.action).

    Handles:
    - Short names: "Write" → "filesystem.write"
    - Double-underscore: "filesystem__write" → "filesystem.write"
    - Already FQN: "filesystem.write" → "filesystem.write"
    - Action-only: "write" → "filesystem.write" (if unambiguous)
    """
    # Already FQN
    if "." in name:
        return name

    # Short API name
    fqn = _SHORT_TO_FQN.get(name)
    if fqn:
        return fqn

    # Double-underscore format
    if "__" in name:
        return name.replace("__", ".")

    # Try lowercase match in short names
    for short, fqn in _SHORT_TO_FQN.items():
        if short.lower() == name.lower():
            return fqn

    # Try action-name match (e.g. "read" → "filesystem.read")
    for fqn in _FQN_TO_SHORT:
        action = fqn.rsplit(".", 1)[-1]
        if action == name or action == name.lower():
            return fqn

    # Check dynamically cached short names (from to_short auto-generation)
    if name in _SHORT_TO_FQN:
        return _SHORT_TO_FQN[name]

    # Can't resolve - return as-is
    return name


def to_short(fqn: str) -> str:
    """Convert a FQN to its short API name.

    "filesystem.write" → "Write"
    "git.commit" → "GitCommit" (auto-generated)
    "http.post" → "HttpPost" (auto-generated)
    """
    short = _FQN_TO_SHORT.get(fqn)
    if short:
        return short
    # Auto-generate: module.action → ModuleAction (PascalCase)
    # This ensures ALL tools get short names, not just the ones we registered
    if "." in fqn:
        module, action = fqn.rsplit(".", 1)
        # Skip common prefixes for cleaner names
        _SKIP_MODULES = {"context_builder", "agent_spawn", "llm_provider"}
        if module in _SKIP_MODULES:
            parts = action.split("_")
            short = "".join(p.capitalize() for p in parts)
        else:
            mod_parts = module.split("_")
            act_parts = action.split("_")
            short = "".join(p.capitalize() for p in mod_parts + act_parts)
        # Cache for reverse lookup
        _FQN_TO_SHORT[fqn] = short
        _SHORT_TO_FQN[short] = fqn
        return short
    return fqn


def is_known_tool(name: str) -> bool:
    """Check if a name (any format) resolves to a known tool."""
    fqn = to_fqn(name)
    return fqn in _FQN_TO_SHORT or "." in fqn
