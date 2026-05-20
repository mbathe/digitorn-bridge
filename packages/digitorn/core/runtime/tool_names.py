"""Centralized tool name resolution."""

from __future__ import annotations

_SHORT_TO_FQN: dict[str, str] = {
    "Read": "filesystem.read",
    "Write": "filesystem.write",
    "Edit": "filesystem.edit",
    "Grep": "filesystem.grep",
    "Glob": "filesystem.glob",
    "Bash": "shell.bash",
    "Remember": "memory.remember",
    "TaskCreate": "memory.task_create",
    "TaskUpdate": "memory.task_update",
    "WebSearch": "web.search",
    "WebFetch": "web.fetch",
    "Agent": "agent_spawn.agent",
    "AskUser": "context_builder.ask_user",
    "BackgroundRun": "context_builder.background_run",
    "WsWrite": "workspace.write",
    "WsRead": "workspace.read",
    "WsEdit": "workspace.edit",
    "WsGlob": "workspace.glob",
    "WsGrep": "workspace.grep",
    "WsDelete": "workspace.delete",
    "PreviewProxy": "web_preview.proxy",
    "PreviewPublish": "web_preview.publish",
    "PreviewDetach": "web_preview.detach",
    "LintCheck": "lsp.diagnostics",
    "LintFile": "lsp.check",
    "NotebookRead": "notebook.read",
    "NotebookEdit": "notebook.edit_cell",
    "NotebookAdd": "notebook.add_cell",
    "NotebookDelete": "notebook.delete_cell",
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

_FQN_TO_SHORT: dict[str, str] = {v: k for k, v in _SHORT_TO_FQN.items()}

_SKIP_MODULES = {"context_builder", "agent_spawn", "llm_provider"}


def to_fqn(name: str) -> str:
    """Resolve any tool name format to its FQN."""
    if "." in name:
        return name

    fqn = _SHORT_TO_FQN.get(name)
    if fqn:
        return fqn

    if "__" in name:
        return name.replace("__", ".")

    for short, fqn in _SHORT_TO_FQN.items():
        if short.lower() == name.lower():
            return fqn

    for fqn in _FQN_TO_SHORT:
        action = fqn.rsplit(".", 1)[-1]
        if action == name or action == name.lower():
            return fqn

    if name in _SHORT_TO_FQN:
        return _SHORT_TO_FQN[name]

    return name


def to_short(fqn: str) -> str:
    """Convert a FQN to its short API name."""
    short = _FQN_TO_SHORT.get(fqn)
    if short:
        return short
    if "." in fqn:
        module, action = fqn.rsplit(".", 1)
        if module in _SKIP_MODULES:
            parts = action.split("_")
            short = "".join(p.capitalize() for p in parts)
        else:
            mod_parts = module.split("_")
            act_parts = action.split("_")
            short = "".join(p.capitalize() for p in mod_parts + act_parts)
        _FQN_TO_SHORT[fqn] = short
        _SHORT_TO_FQN[short] = fqn
        return short
    return fqn


def is_known_tool(name: str) -> bool:
    """Check if a name resolves to a known tool."""
    fqn = to_fqn(name)
    return fqn in _FQN_TO_SHORT or "." in fqn
