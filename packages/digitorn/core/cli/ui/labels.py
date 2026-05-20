"""Tool call labels and resolution - maps tool names to human-readable verbs."""

from __future__ import annotations

from typing import Any

ICON_SUCCESS = "\u2713"  # ✓


TOOL_LABELS: dict[str, tuple[str, str | None]] = {
    # Discovery
    "execute_tool": ("Executing", "name"),
    "search_tools": ("Searching tools", "query"),
    "get_tool": ("Reading tool", "name"),
    "list_categories": ("Listing categories", None),
    "browse_category": ("Browsing", "category"),
    # Parallel / background
    "run_parallel": ("Parallel", None),
    "background_run": ("Background", "name"),
    # Watcher
    "watch_start": ("Starting watcher", "name"),
    "watch_stop": ("Stopping watcher", "watcher_id"),
    "watch_pause": ("Pausing watcher", "watcher_id"),
    "watch_resume": ("Resuming watcher", "watcher_id"),
    "watch_status": ("Watcher status", "watcher_id"),
    "watch_list": ("Listing watchers", None),
    "watch_history": ("Watcher history", "watcher_id"),
    "remember": ("Remembering", "what"),
    # Channels / composition
    "send_notification": ("Sending notification", "channel"),
    "use_skill": ("Using skill", "skill_name"),
    "call_app": ("Calling app", "app_id"),
}


MODULE_ACTION_LABELS: dict[str, tuple[str, str | None]] = {
    # LLM-visible actions
    "database.connect": ("Connecting DB", "database"),
    "database.disconnect": ("Disconnecting DB", None),
    "database.list_connections": ("DB connections", None),
    "database.sql": ("SQL", "query"),
    "database.transaction": ("Tx", "op"),
    "database.bulk_insert": ("Bulk insert", "table"),
    "database.schema": ("Schema", "what"),
    "database.browse": ("Browse", "table"),
    "database.relations": ("Relations", "table"),
    "database.search_data": ("Search", "table"),
    # Internal actions called by RAG/index modules via the bus
    "database.execute_query": ("Executing SQL", "sql"),
    "database.fetch_results": ("Querying", "sql"),
    "database.list_tables": ("Listing tables", None),
    "database.introspect": ("Introspecting DB", None),
    "database.describe": ("Describing", "table"),
    "database.extract_for_index": ("DB extract", None),
    "llm_provider.configure": ("Configure LLM", "provider"),
    "llm_provider.chat": ("LLM chat", "model"),
    "llm_provider.remove": ("Remove LLM", "provider"),
    "llm_provider.list_providers": ("List providers", None),
    "llm_provider.get_provider_info": ("Provider info", "provider"),
    "llm_provider.update_defaults": ("Update LLM defaults", None),
    "queue.create_queue": ("Create queue", "name"),
    "queue.publish": ("Publish", "queue"),
    "queue.subscribe": ("Subscribe", "queue"),
    "queue.unsubscribe": ("Unsubscribe", "queue"),
    "queue.receive": ("Receive", "queue"),
    "queue.ack": ("Acknowledge", "message_id"),
    "queue.nack": ("Reject", "message_id"),
    "queue.peek": ("Peek", "queue"),
    "queue.queue_stats": ("Queue stats", "name"),
    "queue.list_queues": ("List queues", None),
    "queue.delete_queue": ("Delete queue", "name"),
    "queue.purge": ("Purge queue", "name"),
    "queue.dead_letter": ("Dead letters", "queue"),
    "cron_native.schedule": ("Schedule", "when"),
    "cron_native.cancel_schedule": ("Cancel schedule", "job_id"),
    "cron_native.remind": ("Remind me", "when"),
    "index.register_source": ("Register source", "path"),
    "index.register_extractor": ("Register extractor", "name"),
    "index.scan": ("Scanning index", "source"),
    "index.query": ("Index query", "query"),
    "index.relations": ("Index relations", "name"),
    "index.context": ("Index context", "query"),
    "index.invalidate": ("Invalidate index", "source"),
    "vector.add": ("Vector add", "collection"),
    "vector.get": ("Vector get", "collection"),
    "vector.delete": ("Vector delete", "collection"),
    "vector.search": ("Vector search", "query"),
    "vector.count": ("Vector count", "collection"),
    "vector.update_metadata": ("Update metadata", "collection"),
    "http.get": ("GET", "url"),
    "http.post": ("POST", "url"),
    "http.put": ("PUT", "url"),
    "http.patch": ("PATCH", "url"),
    "http.delete": ("DELETE", "url"),
    "http.head": ("HEAD", "url"),
    "http.options": ("OPTIONS", "url"),
    "http.request": ("HTTP request", "url"),
    "http.json_api": ("API call", "url"),
    "http.submit_form": ("Submit form", "url"),
    "http.upload_file": ("Upload file", "url"),
    "http.fetch_page": ("Fetching page", "url"),
    "http.download": ("Downloading", "url"),
    "http.download_status": ("Download status", "download_id"),
    "http.download_cancel": ("Cancel download", "download_id"),
    "http.download_list": ("List downloads", None),
    "mcp.connect": ("Connecting MCP", "server_id"),
    "mcp.disconnect": ("Disconnecting MCP", "server_id"),
    "mcp.reconnect": ("Reconnecting MCP", "server_id"),
    "mcp.list_servers": ("Listing MCP servers", None),
    "mcp.list_tools": ("Listing MCP tools", "server_id"),
    "mcp.call_tool": ("MCP tool", "tool_name"),
    "mcp.list_resources": ("MCP resources", "server_id"),
    "mcp.read_resource": ("MCP read", "uri"),
    "mcp.list_prompts": ("MCP prompts", "server_id"),
    "mcp.get_prompt": ("MCP prompt", "prompt_name"),
    "mcp.health_check": ("MCP health", "server_id"),
    "web.search": ("Web search", "query"),
    "web.fetch": ("Fetching page", "url"),
    "web.extract": ("Extracting", "url"),
    "web.download": ("Downloading", "url"),
    "shell.bash": ("Bash", "command"),
    "shell.bash_background": ("Background", "command"),
    "shell.bash_status": ("Task status", "task_id"),
}


ACTION_LABELS: dict[str, tuple[str, str | None]] = {
    "read": ("Reading", "path"),
    "write": ("Writing", "path"),
    "edit": ("Editing", "path"),
    "ls": ("Listing", "path"),
    "glob": ("Glob", "pattern"),
    "find": ("Glob", "pattern"),
    "grep": ("Searching", "pattern"),
    "mv": ("Moving", "source"),
    "cp": ("Copying", "source"),
    "rm": ("Deleting", "path"),
    "undo": ("Undo", "path"),
    "status": ("Git status", None),
    "diff": ("Git diff", "target"),
    "log": ("Git log", "branch"),
    "blame": ("Git blame", "file"),
    "show": ("Git show", "ref"),
    "branch_list": ("Listing branches", None),
    "add": ("Staging", "files"),
    "commit": ("Committing", "message"),
    "branch_create": ("Creating branch", "name"),
    "checkout": ("Checkout", "target"),
    "stash": ("Stash", "action"),
    "tag": ("Tag", "action"),
    "push": ("Pushing", "branch"),
    "pull": ("Pulling", "branch"),
    "reset": ("Resetting", "ref"),
    "merge": ("Merging", "branch"),
    "pr_create": ("Creating PR", "title"),
    "bash": ("Bash", "command"),
    "bash_background": ("Background", "command"),
    "bash_status": ("Task status", "task_id"),
    "connect": ("Connecting to", "database"),
    "disconnect": ("Disconnecting", None),
    "list_connections": ("Listing connections", None),
    "fetch_results": ("Querying", "sql"),
    "list_tables": ("Listing tables", None),
    "execute_query": ("Executing SQL", "sql"),
    "batch_execute": ("Batch SQL", None),
    "bulk_insert": ("Bulk insert", "table"),
    "upsert": ("Upsert", "table"),
    "get_table_schema": ("Table schema", "table"),
    "table_stats": ("Table stats", "table"),
    "introspect": ("Introspecting", None),
    "describe": ("Describing", "table"),
    "sample": ("Sampling", "table"),
    "annotate": ("Annotating", None),
    "get_annotations": ("Annotations", None),
    "set_policy": ("Set policy", None),
    "get_audit_log": ("Audit log", None),
    "begin_transaction": ("Begin transaction", None),
    "commit_transaction": ("Commit", None),
    "rollback_transaction": ("Rollback", None),
    "explain_query": ("Explain SQL", "sql"),
    "fetch_paginated": ("Paginated query", "sql"),
    "query_history": ("Query history", None),
    "ping": ("Ping", None),
    "schema_diff": ("Schema diff", None),
    "search": ("Searching", "query"),
    "fetch": ("Fetching", "url"),
    "extract": ("Extracting", "url"),
    "download": ("Downloading", "url"),
    "get": ("GET", "url"),
    "post": ("POST", "url"),
    "put": ("PUT", "url"),
    "patch": ("PATCH", "url"),
    "delete": ("DELETE", "url"),
    "head": ("HEAD", "url"),
    "options": ("OPTIONS", "url"),
    "request": ("Request", "url"),
    "json_api": ("API call", "url"),
    "submit_form": ("Submit form", "url"),
    "upload_file": ("Upload file", "url"),
    "fetch_page": ("Fetching page", "url"),
    "download_status": ("Download status", "download_id"),
    "download_cancel": ("Cancel download", "download_id"),
    "download_list": ("List downloads", None),
    "edit_cell": ("Editing cell", "cell_index"),
    "add_cell": ("Adding cell", "cell_type"),
    "delete_cell": ("Deleting cell", "cell_index"),
    "greet": ("Greeting", "name"),
    "say_hello": ("Hello", "name"),
    "greet_many": ("Greeting many", None),
    "spawn_agent": ("Agent", "task"),
    "agent_status": ("Agent status", "agent_id"),
    "agent_result": ("Agent result", "agent_id"),
    "agent_list": ("Listing agents", None),
    "agent_wait": ("Waiting agent", "agent_id"),
    "agent_wait_all": ("Waiting agents", None),
    "agent_cancel": ("Cancel agent", "agent_id"),
    "reassign_agent": ("Reassign agent", "agent_id"),
    "create": ("Create", "output_path"),
    "new_workbook": ("New workbook", "workbook_id"),
    "write_sheet": ("Write sheet", "sheet_name"),
    "write_sheet_from_query": ("Pipe to sheet", "sheet_name"),
    "finalize": ("Finalize", "workbook_id"),
    "read_excel": ("Read Excel", "path"),
    "edit_cells": ("Edit cells", "path"),
    "add_chart": ("Add chart", "chart_type"),
    "add_sheet": ("Add sheet", "sheet_name"),
    "to_csv": ("Export CSV", "path"),
    "from_csv": ("Import CSV", "path"),
    "info": ("Info", "path"),
    "append_rows": ("Append rows", "sheet_name"),
    "add_formulas": ("Add formulas", "sheet_name"),
    "set_conditional_format": ("Conditional format", "sheet_name"),
    "add_chart_to_sheet": ("Add chart", "sheet_name"),
    "workbook_state": ("Workbook state", "workbook_id"),
    "list_workbooks": ("List workbooks", None),
    "create_collection": ("Create collection", "name"),
    "delete_collection": ("Delete collection", "name"),
    "list_collections": ("List collections", None),
    "add_file": ("Index file", "path"),
    "add_directory": ("Index directory", "path"),
    "hybrid_search": ("Hybrid search", "query"),
    "search_multi": ("Multi-search", "query"),
    "collection_stats": ("Collection stats", "collection"),
    "update_metadata": ("Update metadata", "collection"),
    "count": ("Count", "collection"),
    "cache_get": ("Cache get", "key"),
    "cache_set": ("Cache set", "key"),
    "cache_delete": ("Cache delete", "key"),
    "get_or_set": ("Cache get/set", "key"),
    "delete_by_tags": ("Cache invalidate", "tags"),
    "exists": ("Exists", "key"),
    "ttl": ("TTL", "key"),
    "increment": ("Increment", "key"),
    "decrement": ("Decrement", "key"),
    "list_keys": ("List keys", "pattern"),
    "stats": ("Stats", None),
    "clear": ("Clear", None),
    "bulk_get": ("Bulk get", None),
    "bulk_set": ("Bulk set", None),
    "create_queue": ("Create queue", "name"),
    "publish": ("Publish", "queue"),
    "subscribe": ("Subscribe", "queue"),
    "unsubscribe": ("Unsubscribe", "queue"),
    "receive": ("Receive", "queue"),
    "ack": ("Acknowledge", "message_id"),
    "nack": ("Reject", "message_id"),
    "peek": ("Peek", "queue"),
    "queue_stats": ("Queue stats", "name"),
    "list_queues": ("List queues", None),
    "delete_queue": ("Delete queue", "name"),
    "purge": ("Purge queue", "name"),
    "dead_letter": ("Dead letters", "queue"),
    "enqueue": ("Enqueue", "queue"),
    "dequeue": ("Dequeue", "queue"),
    "create_schedule": ("Create schedule", "name"),
    "update_schedule": ("Update schedule", "schedule_id"),
    "delete_schedule": ("Delete schedule", "schedule_id"),
    "list_schedules": ("List schedules", None),
    "schedule_info": ("Schedule info", "schedule_id"),
    "explain_cron": ("Explain cron", "expression"),
    "validate_cron": ("Validate cron", "expression"),
    "next_runs": ("Next runs", "schedule_id"),
    "pause_schedule": ("Pause schedule", "schedule_id"),
    "resume_schedule": ("Resume schedule", "schedule_id"),
    "run_now": ("Run now", "schedule_id"),
    "execution_history": ("Execution history", "schedule_id"),
    "add_dependency": ("Add dependency", "schedule_id"),
    "remove_dependency": ("Remove dependency", "schedule_id"),
    "set_retry_policy": ("Set retry policy", "schedule_id"),
    "set_execution_window": ("Set exec window", "schedule_id"),
    "add_holiday": ("Add holiday", "date"),
    "remove_holiday": ("Remove holiday", "date"),
    "list_holidays": ("List holidays", None),
    "bulk_create": ("Bulk create", None),
    "calendar_view": ("Calendar view", None),
    "register_source": ("Register source", "path"),
    "register_extractor": ("Register extractor", "name"),
    "scan": ("Scanning", "source"),
    "query": ("Querying index", "query"),
    "relations": ("Relations", "name"),
    "context": ("Context", "query"),
    "invalidate": ("Invalidating", "source"),
    "generate": ("Generating", "output_path"),
    "generate_typst": ("PDF (Typst)", "output_path"),
    "list_styles": ("PDF styles", None),
    "read_tables": ("PDF tables", "path"),
    "read_pdf": ("Read PDF", "path"),
    "pdf_info": ("PDF info", "path"),
    "metadata": ("Metadata", "path"),
    "split": ("Splitting", "path"),
    "new_presentation": ("New presentation", "title"),
    "add_slide": ("Add slide", "layout"),
    "edit_slide": ("Edit slide", "slide_index"),
    "remove_slide": ("Remove slide", "index"),
    "reorder_slides": ("Reorder slides", None),
    "preview_slide": ("Preview slide", "slide_index"),
    "presentation_state": ("Presentation state", "presentation_id"),
    "finalize_presentation": ("Finalize presentation", "presentation_id"),
    "list_presentations": ("List presentations", None),
    "list_themes": ("List themes", None),
    "configure": ("Configuring", "provider"),
    "chat": ("LLM chat", "model"),
    "list_providers": ("List providers", None),
    "get_provider_info": ("Provider info", "provider"),
    "update_defaults": ("Update defaults", None),
    "http_get": ("GET", "url"),
    "http_post": ("POST", "url"),
    "http_put": ("PUT", "url"),
    "http_delete": ("DELETE", "url"),
}

# MCP-specific action labels (for dynamic MCP_<server_id> modules)
MCP_ACTION_LABELS: dict[str, tuple[str, str | None]] = {
    "connect": ("Connecting MCP", "server_id"),
    "disconnect": ("Disconnecting MCP", "server_id"),
    "reconnect": ("Reconnecting MCP", "server_id"),
    "list_servers": ("Listing MCP servers", None),
    "list_tools": ("Listing MCP tools", "server_id"),
    "call_tool": ("Calling MCP tool", "tool_name"),
    "list_resources": ("Listing resources", "server_id"),
    "read_resource": ("Reading resource", "uri"),
    "list_prompts": ("Listing prompts", "server_id"),
    "get_prompt": ("Getting prompt", "prompt_name"),
    "health_check": ("MCP health", "server_id"),
}


def _label_from_registry(
    module_name: str, action_name: str, params: dict[str, Any],
) -> tuple[str, str] | None:
    """Try to get CLI label from the module's action registry (dynamic)."""
    try:
        from digitorn.modules.registry import ModuleRegistry

        registry = ModuleRegistry._instance
        if registry is None:
            return None
        module = registry.get(module_name)
        if module is None:
            return None
        entry = module._action_registry.get(action_name)
        if entry is None or entry.spec is None:
            return None
        if entry.spec.cli_label:
            verb = entry.spec.cli_label
            detail = ""
            if entry.spec.cli_param and isinstance(params, dict):
                detail = str(params.get(entry.spec.cli_param, ""))
                if len(detail) > 60:
                    detail = detail[:57] + "..."
            return verb, detail
    except Exception:
        pass  # Non-critical: label rendering is best-effort
    return None


def _memory_label(name: str, params: dict[str, Any]) -> tuple[str, str] | None:
    """Rich labels for memory actions."""
    if not name.startswith("memory."):
        return None

    action = name.split(".", 1)[1] if "." in name else name

    if action == "set_goal":
        goal = params.get("goal", "")
        return "Goal", goal[:60] + "..." if len(goal) > 60 else goal

    if action == "remember":
        content = params.get("content", "")
        return "Remember", content[:60] + "..." if len(content) > 60 else content

    if action == "task_create":
        subject = params.get("subject", "")
        return "+Task", subject[:50] + "..." if len(subject) > 50 else subject

    if action == "task_update":
        status = params.get("status", "")
        tid = params.get("taskId", "")
        icons = {"done": ICON_SUCCESS, "completed": ICON_SUCCESS, "in_progress": ">", "blocked": "!", "pending": " "}
        return "Task", f"{icons.get(status, '')} {tid}"

    return None


_PARAM_PRIORITY = (
    "path", "name", "url", "query", "collection", "key", "command", "sql",
    "table", "file", "workbook_id", "sheet_name", "buffer", "schedule_id",
    "task_id", "agent_id", "watcher_id", "job_id", "queue", "message_id",
    "presentation_id", "download_id", "server_id", "provider", "database",
    "expression", "source", "title", "content", "goal", "pattern",
)


def _first_useful_param(params: dict[str, Any]) -> str:
    """Extract the first useful parameter value for display."""
    if not isinstance(params, dict):
        return ""
    # Extract name from nested spec dicts (e.g. sheet_spec.name)
    for spec_key in ("sheet_spec", "spec"):
        spec = params.get(spec_key)
        if isinstance(spec, dict) and "name" in spec:
            return str(spec["name"])[:60]
    for key in _PARAM_PRIORITY:
        val = params.get(key)
        if val and isinstance(val, str):
            if len(val) > 60:
                val = val[:57] + "..."
            return val
    return ""


def _resolve_detail(params: dict[str, Any], param_key: str | None) -> str:
    """Resolve the detail string from params using the given key, with fallback."""
    if param_key is None:
        return _first_useful_param(params)
    detail = params.get(param_key, "")
    detail_str = str(detail) if detail else ""
    if not detail_str:
        detail_str = _first_useful_param(params)
    if len(detail_str) > 60:
        detail_str = detail_str[:57] + "..."
    return detail_str


def tool_label(name: str, params: dict[str, Any]) -> tuple[str, str]:
    """Return (verb, detail) for a tool call."""
    entry = TOOL_LABELS.get(name)
    if entry is not None:
        verb, param_key = entry
        if name == "execute_tool":
            return _resolve_execute_tool(params)
        if param_key is None:
            return verb, ""
        detail = params.get(param_key, "")
        if name == "search_tools" and detail:
            detail = f'"{detail}"'
        return verb, str(detail) if detail else ""

    module_name = ""
    action_name = ""
    if "__" in name:
        parts = name.split("__", 1)
        module_name = parts[0] if len(parts) > 1 else ""
        action_name = parts[1] if len(parts) > 1 else ""
    elif "." in name:
        parts = name.split(".", 1)
        module_name = parts[0] if len(parts) > 1 else ""
        action_name = parts[1] if len(parts) > 1 else ""

    if action_name:
        # a. Memory module - rich semantic labels
        if module_name == "memory":
            mem_label = _memory_label(f"memory.{action_name}", params)
            if mem_label is not None:
                return mem_label

        # b. Agent spawn - rich labels with specialist/task
        if module_name == "agent_spawn" and action_name == "spawn_agent":
            specialist = params.get("specialist", "")
            task = params.get("task", "")
            if task:
                first_line = task.split("\n")[0]
                if " - " in first_line:
                    task = first_line.split(" - ")[0].strip()
                elif len(first_line) > 60:
                    task = first_line[:57] + "..."
                else:
                    task = first_line
            if specialist:
                return f"Agent ({specialist})", task
            return "Agent", task

        # c. MCP dynamic servers (mcp_<server_id>__action)
        if module_name.startswith("mcp_"):
            server_id = module_name[4:]
            mcp_entry = MCP_ACTION_LABELS.get(action_name)
            if mcp_entry:
                return f"MCP {server_id}", _resolve_detail(params, mcp_entry[1])
            tool_label_text = action_name.replace("_", " ").capitalize()
            return f"MCP {server_id}", tool_label_text

        # d. Module-qualified labels (handles ambiguous names)
        qualified_key = f"{module_name}.{action_name}"
        mod_entry = MODULE_ACTION_LABELS.get(qualified_key)
        if mod_entry is not None:
            return mod_entry[0], _resolve_detail(params, mod_entry[1])

        # e. Dynamic registry lookup (@action cli_label)
        label = _label_from_registry(module_name, action_name, params)
        if label is not None:
            return label

        # f. Generic action labels (bare name)
        action_entry = ACTION_LABELS.get(action_name)
        if action_entry is not None:
            return action_entry[0], _resolve_detail(params, action_entry[1])

        # g. Capitalize fallback
        return action_name.replace("_", " ").capitalize(), _first_useful_param(params)

    action_entry = ACTION_LABELS.get(name)
    if action_entry is not None:
        return action_entry[0], _resolve_detail(params, action_entry[1])

    return name.replace("_", " ").capitalize(), _first_useful_param(params)


def _resolve_execute_tool(params: dict[str, Any]) -> tuple[str, str]:
    """Resolve labels for context_builder.execute_tool (discovery mode)."""
    inner_name = params.get("name", "")
    inner_params = params.get("params", {})
    if isinstance(inner_params, str):
        try:
            import json
            inner_params = json.loads(inner_params)
        except Exception:
            inner_params = {}

    # Memory module - rich labels
    mem_label = _memory_label(inner_name, inner_params)
    if mem_label is not None:
        return mem_label

    # Module-qualified label (e.g., "database.sql" -> "SQL")
    mod_entry = MODULE_ACTION_LABELS.get(inner_name)
    if mod_entry is not None:
        detail = _resolve_detail(inner_params, mod_entry[1]) if isinstance(inner_params, dict) else ""
        return mod_entry[0], detail

    # Extract bare action name
    inner_action = inner_name.split(".")[-1] if "." in inner_name else inner_name

    # Generic action labels
    action_entry = ACTION_LABELS.get(inner_action)
    if action_entry is not None:
        a_verb, a_param = action_entry
        detail = _resolve_detail(inner_params, a_param) if isinstance(inner_params, dict) else ""
        return a_verb, detail

    # Capitalize fallback
    if inner_name:
        detail = _first_useful_param(inner_params) if isinstance(inner_params, dict) else ""
        return inner_action.replace("_", " ").capitalize(), detail

    return "Executing", ""


def result_status(result: Any) -> tuple[bool, str]:
    """Extract (success, error_message) from a tool result."""
    if result is None:
        return True, ""
    if hasattr(result, "success"):
        return result.success, (result.error or "") if hasattr(result, "error") else ""
    if isinstance(result, dict):
        return result.get("success", True), result.get("error", "")
    return True, ""
