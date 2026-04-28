---
id: dev_tools
title: dev_tools
sidebar_label: dev_tools
---

# dev_tools

Three ultra-powerful tools for testing and building Digitorn apps against a live daemon. Design philosophy: **few tools, many modes** - just like `shell` gives you one `Bash` tool with unlimited surface area, `dev_tools` gives you 3 tools that cover everything a Flutter client can do, plus everything the Builder needs to craft and validate apps.

> This module is intended for the **Builder agent** and developer apps. It requires a running daemon and communicates via `DevClient` (HTTP + Socket.IO).

---

## Tools

### `App` - lifecycle, discovery, packages, MCP, drafts, security

Manages apps on the live daemon.

**Visible params:**

| Param | Type | Description |
|-------|------|-------------|
| `yaml_path` | string | Path to app YAML (deploy / validate) |
| `app_id` | string | App ID (status / undeploy / secrets / tools) |

**Hidden params (set via Python / agent tool call):**

| Param | Type | Description |
|-------|------|-------------|
| `yaml_content` | string | Inline YAML content (alternative to yaml_path) |
| `validate_only` | bool | Validate YAML without deploying |
| `compile_yaml` | bool | Compile YAML and return resolved config |
| `prompt_preview` | bool | Preview the resolved system prompt for an agent |
| `agent_id` | string | Agent ID for `prompt_preview` |
| `undeploy` | bool | Undeploy the app |
| `list_apps` | bool | List all deployed apps |
| `list_modules` | bool | List all available modules |
| `list_templates` | bool | List all app templates |
| `list_triggers` | bool | List available trigger types |
| `secret_key` / `secret_value` | string | Set an app secret |
| `credential_provider` / `credential_fields` | string / dict | Create a user-level credential |
| `list_credentials` | bool | List user credentials |
| `delete_credential_id` | string | Delete a user credential |
| `search_tools` | string | Search tools by keyword (empty = list categories) |
| `get_tool` | string | Get full schema of a tool by name |
| `package_source` | string | Install a package from source (git URL / path / registry ID) |
| `list_packages` / `uninstall_package` / `upgrade_package` | bool / string | Package management |
| `mcp_catalog` / `mcp_list` / `mcp_install` / `mcp_delete_id` / `mcp_test_id` | various | MCP server management |
| `list_drafts` / `create_draft_yaml` / `update_draft_id` / `deploy_draft_id` / `delete_draft_id` | various | Builder draft management |
| `security_profile` | bool | Get security profile for `app_id` |
| `health` | bool | Daemon health check |
| `diagnostics` | bool | App diagnostics for `app_id` |

**Typical workflow:**

```python
# Validate
App(yaml_path="app.yaml", validate_only=True)

# Deploy
App(yaml_path="app.yaml")
# → returns app_id, agents, total_tools, required_secrets

# Configure missing secrets
App(app_id="my-app", secret_key="OPENAI_API_KEY", secret_value="sk-...")

# Inspect
App(app_id="my-app")
App(app_id="my-app", search_tools="read")
App(app_id="my-app", get_tool="Write")
App(app_id="my-app", security_profile=True)

# Undeploy
App(app_id="my-app", undeploy=True)
```

---

### `Chat` - sessions, queue, approvals, workspace, live events

Exercises conversational apps like a Flutter user would - plus everything the client shows: live events, queue state, preview snapshot, workspace files, memory, approvals, abort/resume/fork.

**Visible params:**

| Param | Type | Description |
|-------|------|-------------|
| `app_id` | string | App ID (required for first message) |
| `message` | string | Message to send |
| `workspace` | string | Workspace directory path |

**Hidden params (selection):**

| Param | Type | Description |
|-------|------|-------------|
| `session_id` | string | Session ID (follow-ups, inspect) |
| `watch` | bool | Live-stream the turn - returns early on approval/ask_user/error |
| `inspect` | bool | Inspect session: turns, tools used, violations |
| `memory` | bool | Get session memory (goal, facts, entities) |
| `tasks` | bool | Get session task list |
| `history` | bool | Get full message history |
| `get_workspace` | bool | Get workspace snapshot |
| `preview_snapshot` | bool | Get preview UI state |
| `code_snapshot` | bool | Get file tree (no content) |
| `file_path` | string | Read a specific workspace file |
| `approve_file` / `reject_file` | string | Approve / reject a workspace file |
| `queue` / `clear_queue` / `cancel_entry_id` | various | Queue management |
| `abort` / `resume` / `fork` / `compact` | bool | Session control |
| `pending` | bool | List pending approvals and ask_user questions |
| `respond` | string | Answer an `ask_user` question |
| `approve_id` / `deny_id` | string | Approve / deny a pending tool call |
| `list_sessions` | bool | List all sessions of `app_id` |
| `search` | string | Search sessions by query |

**Watch mode (recommended for testing):**

```python
# Non-blocking - returns early on blockers
Chat(app_id="my-app", message="Refactor the auth module", watch=True)
# Returns:
# {
#   "session_id": "...",
#   "status": "completed" | "pending_approval" | "pending_ask_user" | "error" | "timeout",
#   "text": "...",
#   "tool_calls": [...],
#   "timeline": [...]
# }
```

**Typical multi-turn test:**

```python
# Turn 1
Chat(app_id="my-app", message="List files in src/")
# → session_id

# Turn 2
Chat(session_id="s123", message="Edit main.py to add logging")

# Inspect
Chat(session_id="s123", inspect=True)
# → tools_used, files_read, files_edited, behavior_violations

# Memory
Chat(session_id="s123", memory=True)
```

---

### `Run` - one-shot, pipeline, triggers, background sessions/tasks

Non-conversational execution: one-shot apps, pipelines, triggers, background sessions, background tasks, watchers.

**Visible params:**

| Param | Type | Description |
|-------|------|-------------|
| `app_id` | string | App ID (required) |
| `input_text` | string | Input for one-shot apps |

**Hidden params (selection):**

| Param | Type | Description |
|-------|------|-------------|
| `pipeline` / `pipeline_input` | bool / any | Run as pipeline with structured input |
| `trigger_id` | string | Fire a trigger by ID |
| `test_trigger` | bool | Dry-run the trigger |
| `trigger_payload` | dict | Payload for `fire_trigger` |
| `background_message` / `background_payload` | string / dict | Create a background session |
| `list_bg_sessions` / `bg_session_id` / `bg_pause_id` / `bg_resume_id` | various | Background session management |
| `create_bg_task` / `list_bg_tasks` / `bg_task_id` / `wait_bg_task` / `cancel_bg_task_id` | various | Background task management |
| `list_watchers` / `create_watcher` | various | Watcher management |
| `activations` | bool | List activation history |
| `errors` | bool | List app errors |

**Examples:**

```python
# One-shot
Run(app_id="research", input_text="Compare React vs Vue for 2025")

# Pipeline
Run(app_id="etl", pipeline=True, pipeline_input={"urls": ["https://..."]})

# Fire a trigger
Run(app_id="notifier", trigger_id="daily-report", trigger_payload={"date": "2026-04-26"})

# Background session
Run(app_id="monitor", background_message="Watch for anomalies in the last hour")

# Wait on a background task
Run(app_id="batch", bg_task_id="t_abc", wait_bg_task=True)
```

---

## When to use which tool

| Tool | Use when |
|------|----------|
| `App` | App lifecycle, discovery, secrets, packages, MCP, builder drafts |
| `Chat` | `mode: conversation` apps - multi-turn, interactive, inspect/debug |
| `Run` | `mode: one_shot`, `pipeline`, `background` - non-interactive execution |

---

## Testing workflow

```python
# 1. Validate YAML
App(yaml_path="app.yaml", validate_only=True)

# 2. Deploy
App(yaml_path="app.yaml")
# Check required_secrets in the response

# 3. Set missing secrets
App(app_id="my-app", secret_key="KEY", secret_value="value")

# 4. Smoke test (watch mode avoids timeout)
Chat(app_id="my-app", message="<realistic task>", watch=True)

# 5. Multi-turn check
Chat(session_id="...", message="<follow-up>")

# 6. Inspect
Chat(session_id="...", inspect=True)
# Verify: tools_used, used_bash_for_files, behavior_violations

# 7. Fix → redeploy → retest if needed
```

**Rules:**
- Always validate before deploying.
- Always check `required_secrets` after deploy - the app won't work without them.
- Use realistic messages, not `"test"`.
- Use `watch=True` for testing to avoid timeouts and get early blocker detection.
- Always inspect after a test turn.

---

## YAML usage

```yaml
modules:
  dev_tools: {}    # no config needed

agents:
  - id: builder
    modules:
      - dev_tools    # full access to App, Chat, Run
```

---

## Source

`packages/digitorn/modules/dev_tools/module.py` - `DevToolsModule` (VERSION 3.0.0)
