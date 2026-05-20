"""Tool call display metadata - single source of truth for client rendering."""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


DISPLAY_DEFAULTS: dict[str, Any] = {
    "version": 2,
    "icons": {
        "file":        "description",
        "folder":      "folder_rounded",
        "checklist":   "checklist",
        "memory":      "psychology",
        "terminal":    "terminal",
        "search":      "search",
        "agent":       "smart_toy",
        "web":         "public",
        "database":    "storage",
        "git":         "commit",
        "tool":        "build",
        "image":       "image",
        "network":     "language",
        "edit":        "edit",
        "preview":     "preview",
        "workspace":   "workspaces",
        "diagnostics": "troubleshoot",
        "shell":       "terminal",
    },
    "channels": {
        "chat":        {"label": "Chat",        "panel": "chat",        "icon": "tool"},
        "tasks":       {"label": "Tasks",       "panel": "tasks",       "icon": "checklist"},
        "memory":      {"label": "Memory",      "panel": "memory",      "icon": "memory"},
        "agents":      {"label": "Agents",      "panel": "agents",      "icon": "agent"},
        "workspace":   {"label": "Workspace",   "panel": "workspace",   "icon": "workspace"},
        "terminal":    {"label": "Terminal",    "panel": "terminal",    "icon": "terminal"},
        "diagnostics": {"label": "Diagnostics", "panel": "diagnostics", "icon": "diagnostics"},
        "preview":     {"label": "Preview",     "panel": "preview",     "icon": "preview"},
        "none":        {"label": "Internal",    "panel": None,          "icon": "tool"},
    },
    "fallbacks": {
        "patterns": [
            # Memory / todo / goal tooling
            {"match": r"^task_(create|update|delete|complete)$",
             "channel": "tasks",   "hidden": True,  "icon": "checklist", "category": "action"},
            {"match": r"^(memory|mem)_",
             "channel": "memory",  "hidden": True,  "icon": "memory",    "category": "memory"},
            {"match": r"^(set_goal|remember)$",
             "channel": "memory",  "hidden": True,  "icon": "memory",    "category": "memory"},

            # legacy split-action FQNs only; bare `agent` is decided in build_display step 2.6 from params.
            {"match": r"^(spawn_agent|agent_(wait|wait_all|result|status|cancel|list))$",
             "channel": "agents",  "hidden": True,  "icon": "agent",     "category": "control_flow"},
            {"match": r"^reassign_agent$",
             "channel": "agents",  "hidden": True,  "icon": "agent",     "category": "control_flow"},

            # Tool discovery / introspection
            {"match": r"^(search_tools|get_tool|list_categories|browse_category|execute_tool)$",
             "channel": "chat",    "hidden": True,  "icon": "search",    "category": "plumbing"},

            # Shell
            {"match": r"^(bash|bash_background|shell)$",
             "channel": "terminal","hidden": False, "icon": "terminal",  "category": "action"},

            # Filesystem
            {"match": r"^(read|write|edit|delete|mkdir|mv|cp|ls|rm|touch|stat)$",
             "channel": "workspace","hidden": False,"icon": "file",      "category": "action"},

            # Network / web
            {"match": r"^(http|fetch|get|post|put|patch)$",
             "channel": "chat",    "hidden": False, "icon": "network",   "category": "action"},

            # Database
            {"match": r"^(sql|query|browse|schema|bulk_insert|transaction)$",
             "channel": "chat",    "hidden": False, "icon": "database",  "category": "action"},

            # Preview / widget
            {"match": r"^(set_state|set_resource|patch_resource|push_node|push_edge|clear_channel|bulk_set_resources)$",
             "channel": "preview", "hidden": True,  "icon": "preview",   "category": "plumbing"},
        ],
    },
}


# Central FQN → display field table. Resolution: ActionSpec → STATIC_OVERRIDES[fqn] → [bare_name] → labels → regex → defaults.
# Partial entries fall through field-by-field.

_H = True

STATIC_OVERRIDES: dict[str, dict[str, Any]] = {

    # ══ agent_spawn - lifecycle ops, panel: agents ══════════════════
    "agent_spawn.spawn_agent":    {"channel": "agents", "icon": "agent", "category": "control_flow"},
    # agent_spawn.agent is the 8-mode entrypoint - hidden is decided per-call in build_display from params.
    "agent_spawn.agent":          {"channel": "agents", "icon": "agent", "category": "control_flow"},
    "agent_spawn.agent_wait":     {"channel": "agents", "hidden": _H, "icon": "agent", "category": "control_flow"},
    "agent_spawn.agent_wait_all": {"channel": "agents", "hidden": _H, "icon": "agent", "category": "control_flow"},
    "agent_spawn.agent_result":   {"channel": "agents", "hidden": _H, "icon": "agent", "category": "control_flow"},
    "agent_spawn.agent_status":   {"channel": "agents", "hidden": _H, "icon": "agent", "category": "control_flow"},
    "agent_spawn.agent_cancel":   {"channel": "agents", "hidden": _H, "icon": "agent", "category": "control_flow"},
    "agent_spawn.agent_list":     {"channel": "agents", "hidden": _H, "icon": "agent", "category": "control_flow"},
    "agent_spawn.reassign_agent": {"channel": "agents", "hidden": _H, "icon": "agent", "category": "control_flow"},

    # ══ memory - panels: memory + tasks ═════════════════════════════
    "memory.set_goal":    {"channel": "memory", "hidden": _H, "icon": "memory",    "category": "memory",  "verb": "Goal"},
    "memory.remember":    {"channel": "memory", "hidden": _H, "icon": "memory",    "category": "memory",  "verb": "Remember"},
    "memory.task_create": {"channel": "tasks",  "hidden": _H, "icon": "checklist", "category": "action",  "verb": "Create task"},
    "memory.task_update": {"channel": "tasks",  "hidden": _H, "icon": "checklist", "category": "action",  "verb": "Update task"},

    # cache module removed

    # ══ preview - SSE routed to preview iframe ══════════════════════
    "preview.set_state":         {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "preview.patch_state":       {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "preview.get_state":         {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "preview.clear":             {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "preview.set_resource":      {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "preview.patch_resource":    {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "preview.delete_resource":   {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "preview.list_resources":    {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "preview.bulk_set_resources":{"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "preview.clear_channel":     {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "preview.emit":              {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "preview.push_node":         {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "preview.push_edge":         {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "preview.update_node":       {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "preview.highlight_node":    {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "preview.remove_node":       {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "preview.remove_edge":       {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},

    # ══ widget - has dedicated widget_* SSE events ══════════════════
    "widget.render":     {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "widget.update":     {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "widget.close":      {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "widget.clear":      {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "widget.set_state":  {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "widget.get_state":  {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},
    "widget.error":      {"channel": "preview", "hidden": _H, "icon": "preview", "category": "plumbing"},

    # ══ lsp - diagnostics panel, always silent ══════════════════════
    "lsp.check": {"channel": "diagnostics", "hidden": _H, "icon": "diagnostics", "category": "plumbing"},

    # ══ index - internal tool index (all internal) ══════════════════
    "index.clear":           {"channel": "none", "hidden": _H, "category": "plumbing"},
    "index.rebuild":         {"channel": "none", "hidden": _H, "category": "plumbing"},
    "index.sync":            {"channel": "none", "hidden": _H, "category": "plumbing"},
    "index.get_index":       {"channel": "none", "hidden": _H, "category": "plumbing"},
    "index.list_all":        {"channel": "none", "hidden": _H, "category": "plumbing"},
    "index.query":           {"channel": "none", "hidden": _H, "category": "plumbing"},
    "index.extract_by_path": {"channel": "none", "hidden": _H, "category": "plumbing"},

    # ══ llm_provider - management, hidden ═══════════════════════════
    "llm_provider.list_providers":    {"hidden": _H, "category": "plumbing"},
    "llm_provider.get_provider_info": {"hidden": _H, "category": "plumbing"},
    "llm_provider.remove":            {"hidden": _H, "category": "plumbing"},

    # ══ context_builder - meta-tools, discovery, plumbing ═══════════
    "context_builder.search_tools":       {"hidden": _H, "icon": "search", "category": "plumbing"},
    "context_builder.get_tool":           {"hidden": _H, "icon": "search", "category": "plumbing"},
    "context_builder.list_categories":    {"hidden": _H, "icon": "search", "category": "plumbing"},
    "context_builder.browse_category":    {"hidden": _H, "icon": "search", "category": "plumbing"},
    "context_builder.execute_tool":       {"hidden": _H, "icon": "tool",   "category": "plumbing"},
    "context_builder.run_parallel":       {"hidden": _H, "icon": "tool",   "category": "control_flow"},
    "context_builder.remember":           {"channel": "memory", "hidden": _H, "icon": "memory", "category": "memory"},
    # background - 1 tool, 5 modes via hidden params
    "context_builder.background_run":     {"verb": "Background", "icon": "tool"},
    # watchers - write visible, queries hidden
    "context_builder.watch_stop":    {"hidden": _H, "category": "plumbing"},
    "context_builder.watch_pause":   {"hidden": _H, "category": "plumbing"},
    "context_builder.watch_resume":  {"hidden": _H, "category": "plumbing"},
    "context_builder.watch_status":  {"hidden": _H, "category": "plumbing"},
    "context_builder.watch_list":    {"hidden": _H, "category": "plumbing"},
    "context_builder.watch_history": {"hidden": _H, "category": "plumbing"},
    # ask_user stays visible (critical user-facing action)
    "context_builder.ask_user":    {"verb": "Ask user", "icon": "tool", "category": "action"},

    # ══ filesystem - workspace panel, visible ═══════════════════════
    "filesystem.read":      {"channel": "workspace", "icon": "file",   "group": "filesystem", "verb": "Read"},
    "filesystem.write":     {"channel": "workspace", "icon": "file",   "group": "filesystem", "verb": "Write"},
    "filesystem.edit":      {"channel": "workspace", "icon": "edit",   "group": "filesystem", "verb": "Edit"},
    "filesystem.mkdir":     {"channel": "workspace", "icon": "folder", "group": "filesystem", "verb": "Mkdir"},
    "filesystem.mv":        {"channel": "workspace", "icon": "file",   "group": "filesystem", "verb": "Move"},
    "filesystem.cp":        {"channel": "workspace", "icon": "file",   "group": "filesystem", "verb": "Copy"},
    "filesystem.ls":        {"channel": "workspace", "icon": "folder", "group": "filesystem", "verb": "List"},
    "filesystem.glob":      {"channel": "workspace", "icon": "search", "group": "filesystem", "verb": "Glob"},
    "filesystem.find":      {"channel": "workspace", "icon": "search", "group": "filesystem", "verb": "Find"},
    "filesystem.grep":      {"channel": "workspace", "icon": "search", "group": "filesystem", "verb": "Grep"},
    # info-only / internal → hidden
    "filesystem.file_stat": {"hidden": _H, "category": "plumbing"},
    "filesystem.delete":    {"hidden": _H, "category": "plumbing"},
    "filesystem.insert":    {"hidden": _H, "category": "plumbing"},
    "filesystem.undo":      {"hidden": _H, "category": "plumbing"},

    # code module removed - react-sandbox uses preview.set_resource("files", …)

    # ══ shell - terminal panel ══════════════════════════════════════
    "shell.bash":            {"channel": "terminal", "icon": "terminal", "group": "shell", "verb": "Bash"},
    "shell.bash_background": {"channel": "terminal", "icon": "terminal", "group": "shell", "verb": "Bash (bg)"},
    "shell.bash_status":     {"hidden": _H, "category": "plumbing"},

    # ══ http - visible network calls ═══════════════════════════════
    "http.get":         {"icon": "network", "group": "http", "verb": "GET"},
    "http.post":        {"icon": "network", "group": "http", "verb": "POST"},
    "http.put":         {"icon": "network", "group": "http", "verb": "PUT"},
    "http.patch":       {"icon": "network", "group": "http", "verb": "PATCH"},
    "http.delete":      {"icon": "network", "group": "http", "verb": "DELETE"},
    "http.head":        {"icon": "network", "group": "http", "verb": "HEAD"},
    "http.options":     {"icon": "network", "group": "http", "verb": "OPTIONS"},
    "http.request":     {"icon": "network", "group": "http", "verb": "HTTP"},
    "http.json_api":    {"icon": "network", "group": "http", "verb": "API"},
    "http.submit_form": {"icon": "network", "group": "http", "verb": "Submit form"},
    "http.upload_file": {"icon": "network", "group": "http", "verb": "Upload"},
    "http.fetch_page":  {"icon": "web",     "group": "http", "verb": "Fetch page"},
    "http.download":    {"icon": "network", "group": "http", "verb": "Download"},
    "http.download_status": {"hidden": _H, "category": "plumbing"},
    "http.download_list":   {"hidden": _H, "category": "plumbing"},
    "http.download_cancel": {"hidden": _H, "category": "plumbing"},

    # ══ web - visible ══════════════════════════════════════════════
    "web.search":   {"icon": "search", "verb": "Web search"},
    "web.fetch":    {"icon": "web",    "verb": "Fetch"},
    "web.extract":  {"icon": "web",    "verb": "Extract"},
    "web.download": {"icon": "network","verb": "Download"},

    # ══ database - writes/queries visible, mgmt hidden ═════════════
    "database.sql":          {"icon": "database", "group": "database", "verb": "SQL"},
    "database.transaction":  {"icon": "database", "group": "database", "verb": "Tx"},
    "database.bulk_insert":  {"icon": "database", "group": "database", "verb": "Bulk insert"},
    "database.browse":       {"icon": "database", "group": "database", "verb": "Browse"},
    "database.schema":       {"icon": "database", "group": "database", "verb": "Schema"},
    "database.relations":    {"icon": "database", "group": "database", "verb": "Relations"},
    "database.search_data":  {"icon": "database", "group": "database", "verb": "Search data"},
    "database.connect":          {"hidden": _H, "category": "plumbing"},
    "database.disconnect":       {"hidden": _H, "category": "plumbing"},
    "database.list_connections": {"hidden": _H, "category": "plumbing"},

    # git module removed - all git ops go through shell.bash

    # ══ mcp - call_tool visible with "MCP:" prefix, rest hidden ════
    "mcp.call_tool":     {"verb": "MCP",  "icon": "tool", "group": "mcp"},
    "mcp.connect":       {"hidden": _H, "category": "plumbing"},
    "mcp.disconnect":    {"hidden": _H, "category": "plumbing"},
    "mcp.reconnect":     {"hidden": _H, "category": "plumbing"},
    "mcp.list_servers":  {"hidden": _H, "category": "plumbing"},
    "mcp.list_tools":    {"hidden": _H, "category": "plumbing"},
    "mcp.list_resources":{"hidden": _H, "category": "plumbing"},
    "mcp.list_prompts":  {"hidden": _H, "category": "plumbing"},
    "mcp.get_prompt":    {"hidden": _H, "category": "plumbing"},
    "mcp.read_resource": {"hidden": _H, "category": "plumbing"},
    "mcp.health_check":  {"hidden": _H, "category": "plumbing"},

    # browser and computer_use modules removed

    # pdf module removed - RAG inlines pymupdf directly for ingestion

    # presentation module removed

    # notebook and spreadsheet modules removed
    # (RAG inlines openpyxl + csv directly for spreadsheet ingestion)

    # ══ rag - writes visible, queries visible, info hidden ════════
    "rag.query":                  {"icon": "search",   "group": "rag", "verb": "Query KB"},
    "rag.multi_query":            {"icon": "search",   "group": "rag", "verb": "Multi-query KB"},
    "rag.sql_query":              {"icon": "search",   "group": "rag", "verb": "SQL KB"},
    "rag.ingest":                 {"icon": "database", "group": "rag", "verb": "Ingest"},
    "rag.ingest_file":            {"icon": "database", "group": "rag", "verb": "Ingest file"},
    "rag.ingest_directory":       {"icon": "database", "group": "rag", "verb": "Ingest dir"},
    "rag.ingest_database":        {"icon": "database", "group": "rag", "verb": "Ingest DB"},
    "rag.create_knowledge_base":  {"icon": "database", "group": "rag", "verb": "New KB"},
    "rag.delete_knowledge_base":  {"icon": "database", "group": "rag", "verb": "Delete KB"},
    "rag.migrate_embeddings":     {"icon": "database", "group": "rag", "verb": "Migrate KB"},
    "rag.list_knowledge_bases":   {"hidden": _H, "category": "plumbing"},
    "rag.knowledge_base_stats":   {"hidden": _H, "category": "plumbing"},
    "rag.list_models":            {"hidden": _H, "category": "plumbing"},
    "rag.clear_cache":            {"hidden": _H, "category": "plumbing"},

    # ══ vector - writes + search visible, info hidden ════════════
    "vector.add":               {"icon": "database", "group": "vector", "verb": "Embed"},
    "vector.add_file":          {"icon": "database", "group": "vector", "verb": "Embed file"},
    "vector.search":            {"icon": "search",   "group": "vector", "verb": "Search"},
    "vector.hybrid_search":     {"icon": "search",   "group": "vector", "verb": "Hybrid search"},
    "vector.create_collection": {"icon": "database", "group": "vector", "verb": "New collection"},
    "vector.delete_collection": {"icon": "database", "group": "vector", "verb": "Delete collection"},
    "vector.delete":            {"icon": "database", "group": "vector", "verb": "Delete docs"},
    "vector.update_metadata":   {"icon": "database", "group": "vector", "verb": "Update metadata"},
    "vector.list_collections":  {"hidden": _H, "category": "plumbing"},
    "vector.collection_stats":  {"hidden": _H, "category": "plumbing"},
    "vector.count":             {"hidden": _H, "category": "plumbing"},
    "vector.get":               {"hidden": _H, "category": "plumbing"},

    # ══ queue - publish/consume visible ══════════════════════════
    "queue.publish":      {"icon": "network", "group": "queue", "verb": "Publish"},
    "queue.receive":      {"icon": "network", "group": "queue", "verb": "Receive"},
    "queue.subscribe":    {"icon": "network", "group": "queue", "verb": "Subscribe"},
    "queue.ack":          {"icon": "network", "group": "queue", "verb": "Ack"},
    "queue.nack":         {"icon": "network", "group": "queue", "verb": "Nack"},
    "queue.create_queue": {"icon": "network", "group": "queue", "verb": "New queue"},
    "queue.delete_queue": {"icon": "network", "group": "queue", "verb": "Delete queue"},
    "queue.purge":        {"icon": "network", "group": "queue", "verb": "Purge"},
    "queue.list_queues":  {"hidden": _H, "category": "plumbing"},
    "queue.queue_stats":  {"hidden": _H, "category": "plumbing"},
    "queue.peek":         {"hidden": _H, "category": "plumbing"},
    "queue.dead_letter":  {"hidden": _H, "category": "plumbing"},
    "queue.unsubscribe":  {"hidden": _H, "category": "plumbing"},

    # ══ channels - user-facing messaging visible ════════════════
    "channels.send_message":      {"icon": "network", "group": "channels", "verb": "Send message"},
    "channels.broadcast":         {"icon": "network", "group": "channels", "verb": "Broadcast"},
    "channels.reply":             {"icon": "network", "group": "channels", "verb": "Reply"},
    "channels.list_providers":    {"hidden": _H, "category": "plumbing"},
    "channels.provider_status":   {"hidden": _H, "category": "plumbing"},
    "channels.provider_history":  {"hidden": _H, "category": "plumbing"},
    "channels.stats":             {"hidden": _H, "category": "plumbing"},
    "channels.simulate_event":    {"hidden": _H, "category": "plumbing"},
    "channels.test_send":         {"hidden": _H, "category": "plumbing"},
    "channels.pause_provider":    {"hidden": _H, "category": "plumbing"},
    "channels.resume_provider":   {"hidden": _H, "category": "plumbing"},

    # ══ cron_native - 3 actions ═════════════════
    "cron_native.schedule":         {"icon": "tool", "group": "cron", "verb": "Schedule"},
    "cron_native.cancel_schedule":  {"icon": "tool", "group": "cron", "verb": "Cancel schedule"},
    "cron_native.remind":           {"icon": "tool", "group": "cron", "verb": "Remind me"},
}


# Compiled regex cache for fallback patterns (avoids recompile per call).
_FALLBACK_REGEX_CACHE: list[tuple[re.Pattern[str], dict[str, Any]]] = []


def _get_fallback_regexes() -> list[tuple[re.Pattern[str], dict[str, Any]]]:
    global _FALLBACK_REGEX_CACHE
    if not _FALLBACK_REGEX_CACHE:
        for entry in DISPLAY_DEFAULTS["fallbacks"]["patterns"]:
            try:
                pat = re.compile(entry["match"])
            except re.error as exc:
                logger.warning("bad display fallback regex %r: %s", entry.get("match"), exc)
                continue
            meta = {k: v for k, v in entry.items() if k != "match"}
            _FALLBACK_REGEX_CACHE.append((pat, meta))
    return _FALLBACK_REGEX_CACHE


def _bare_action(name: str) -> str:
    """Return the bare action name from a qualified / short tool name."""
    if not name:
        return ""
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    elif "__" in name:
        name = name.rsplit("__", 1)[-1]
    return name.lower()


def _truncate(s: Any, limit: int = 60) -> str:
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def build_display(
    name: str,
    params: dict[str, Any] | None,
    action_spec: Any | None = None,
) -> dict[str, Any]:
    """Compute the `display` dict for a single tool call."""
    params = params or {}
    display: dict[str, Any] = {
        "verb": "",
        "detail": "",
        "icon": "",
        "channel": "",
        "hidden": False,
        "category": "",
        "group": "",
        "visible_params": None,
    }

    if action_spec is not None:
        if getattr(action_spec, "display_verb", ""):
            display["verb"] = action_spec.display_verb
        elif getattr(action_spec, "cli_label", ""):
            display["verb"] = action_spec.cli_label

        detail_param = (
            getattr(action_spec, "display_detail_param", "")
            or getattr(action_spec, "cli_param", "")
        )
        if detail_param and detail_param in params:
            display["detail"] = _truncate(params.get(detail_param))

        for field in ("display_icon", "display_channel", "display_category", "display_group"):
            val = getattr(action_spec, field, "")
            if val:
                display[field.replace("display_", "")] = val
        if getattr(action_spec, "display_hidden", False):
            display["hidden"] = True

        # Extract visible params from input_schema (exclude hidden ones)
        schema = getattr(action_spec, "input_schema", None)
        if isinstance(schema, dict):
            props = schema.get("properties") or {}
            if isinstance(props, dict):
                display["visible_params"] = [
                    k for k, v in props.items()
                    if isinstance(v, dict) and not v.get("hidden")
                ]

    fqn = name
    if name and "__" in name:
        fqn = name.replace("__", ".", 1)
    override = STATIC_OVERRIDES.get(fqn)
    if override is None:
        try:
            from digitorn.core.runtime.tool_names import to_fqn as _to_fqn
            resolved_fqn = _to_fqn(name)
            if resolved_fqn != name:
                override = STATIC_OVERRIDES.get(resolved_fqn)
        except Exception as exc:
            logger.debug("tool_display best-effort block failed: %s", exc)
    if override is None:
        override = STATIC_OVERRIDES.get(_bare_action(name))
    if override:
        for field in ("verb", "detail", "icon", "channel", "category", "group"):
            if not display.get(field) and override.get(field):
                display[field] = override[field]
        if override.get("hidden") and not display["hidden"]:
            display["hidden"] = True

    # shell.bash multiplexes 8 modes - override verb/detail on plumbing modes so a status check doesn't render as a failed command.
    fqn_resolved = name
    if name and "__" in name:
        fqn_resolved = name.replace("__", ".", 1)
    if fqn_resolved == "shell.bash" or _bare_action(name) == "bash":
        tid = params.get("task_id")
        if isinstance(tid, str) and tid:
            if params.get("kill") is True:
                display["verb"] = "Bash kill"
            elif params.get("wait") is True:
                display["verb"] = "Bash wait"
            elif params.get("stdin_text") is not None:
                display["verb"] = "Bash stdin"
            elif not params.get("command"):
                display["verb"] = "Bash status"
            display["detail"] = _truncate(tid)
            display["category"] = display["category"] or "plumbing"
        else:
            # prefer description over command for the chip header - the 60-char truncation makes long commands unreadable.
            desc = params.get("description")
            if isinstance(desc, str) and desc.strip():
                display["detail"] = _truncate(desc.strip())

    if fqn_resolved == "agent_spawn.agent" or _bare_action(name) == "agent":
        prompt = params.get("prompt")
        agent_id = params.get("agent_id")
        agent_ids = params.get("agent_ids")
        list_agents = params.get("list_agents")
        cancel = params.get("cancel")
        wait_flag = params.get("wait")
        reassign = params.get("reassign")
        if list_agents is True:
            display["verb"] = "Agent list"
        elif agent_id and cancel is True:
            display["verb"] = "Agent cancel"
            display["detail"] = _truncate(str(agent_id))
        elif agent_id and isinstance(reassign, str) and reassign:
            display["verb"] = "Agent reassign"
            display["detail"] = _truncate(reassign)
        elif agent_id and wait_flag is True:
            display["verb"] = "Agent wait"
            display["detail"] = _truncate(str(agent_id))
        elif isinstance(agent_ids, list) and agent_ids:
            display["verb"] = "Agent wait"
            display["detail"] = (
                f"{len(agent_ids)} agent" + ("" if len(agent_ids) == 1 else "s")
            )
        elif agent_id and not prompt:
            display["verb"] = "Agent status"
            display["detail"] = _truncate(str(agent_id))
        elif isinstance(prompt, str) and prompt:
            display["verb"] = "Agent"
            display["detail"] = _truncate(prompt)
        # Force hidden for every mode - AgentGroup owns the rendering.
        display["hidden"] = True
        display["category"] = "control_flow"

    if not display["verb"] or not display["detail"]:
        try:
            from digitorn.core.cli.ui.labels import tool_label
            legacy_verb, legacy_detail = tool_label(name, params)
            if not display["verb"] and legacy_verb:
                display["verb"] = legacy_verb
            if not display["detail"] and legacy_detail:
                display["detail"] = _truncate(legacy_detail)
        except Exception as exc:
            logger.debug("tool_label fallback failed for %s: %s", name, exc)

    bare = _bare_action(name)
    for pat, meta in _get_fallback_regexes():
        if not pat.search(bare):
            continue
        for field in ("channel", "icon", "category", "group"):
            if not display.get(field) and meta.get(field):
                display[field] = meta[field]
        if not display["hidden"] and meta.get("hidden"):
            display["hidden"] = True
        break

    if not display["verb"]:
        display["verb"] = bare.replace("_", " ").title() or "Tool"
    if not display["channel"]:
        display["channel"] = "chat"
    if not display["category"]:
        display["category"] = "action"
    if not display["icon"]:
        display["icon"] = "tool"

    return display
