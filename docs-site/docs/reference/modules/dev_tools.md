---
id: dev_tools
title: dev_tools Module
sidebar_label: dev_tools
---

# dev_tools

Three tools for testing and building Digitorn apps against
a live daemon. Same design as `shell` (one `Bash`, many
modes): `dev_tools` exposes three tools whose hidden params
cover everything a the chat client / web client can do, plus what
the Builder agent needs to write and validate apps.

| Property | Value |
|----------|-------|
| Module id | `dev_tools` |
| Version | `3.0.0` |
| Action count | 3 (LLM-exposed) |
| Type | user (intended for the Builder agent + developer apps) |
| Transport | `DevClient` (HTTP + Socket.IO against a running daemon). |

> Requires a **running daemon**. Communicates via
> `DevClient` against `http://localhost:8000` by default.

## The 3 actions

| Tool | FQN | Purpose |
|------|-----|---------|
| `App` | `dev_tools.app` | App lifecycle + discovery + secrets + packages + MCP + Builder drafts + security. |
| `Chat` | `dev_tools.chat` | Drive conversational apps - sessions, queue, approvals, workspace, live events. |
| `Run` | `dev_tools.run` | Non-conversational execution - one-shot, pipeline, triggers, background sessions / tasks, watchers. |

### When to use which

| Tool | Use for |
|------|---------|
| `App` | Lifecycle, discovery, secrets, packages, MCP, Builder drafts. |
| `Chat` | `runtime.mode: conversation` apps - multi-turn, interactive, inspect / debug. |
| `Run` | `runtime.mode: one_shot` / `pipeline` / `background` - non-interactive execution. |

## `App` - lifecycle + discovery

Visible params:

| Param | Description |
|-------|-------------|
| `yaml_path` | Path to app YAML (deploy / validate). |
| `app_id` | App ID (status / undeploy / secrets / tools). |

Hidden params (selection - see for the full list):

- **Validate / deploy** - `yaml_content`, `validate_only`,
  `compile_yaml`, `prompt_preview`, `agent_id`, `undeploy`.
- **Discovery** - `list_apps`, `list_modules`,
  `list_templates`, `list_triggers`, `search_tools`,
  `get_tool`.
- **Secrets** - `secret_key`, `secret_value`.
- **Credentials** - `credential_provider`,
  `credential_fields`, `list_credentials`,
  `delete_credential_id`.
- **Packages** - `package_source`, `list_packages`,
  `uninstall_package`, `upgrade_package`.
- **MCP** - `mcp_catalog`, `mcp_list`, `mcp_install`,
  `mcp_delete_id`, `mcp_test_id`.
- **Builder drafts** - `list_drafts`, `create_draft_yaml`,
  `update_draft_id`, `deploy_draft_id`, `delete_draft_id`.
- **Inspect** - `security_profile`, `health`, `diagnostics`.

Typical flow:

```python
# Validate
App(yaml_path="app.yaml", validate_only=True)

# Deploy → returns app_id, agents, total_tools, required_secrets
App(yaml_path="app.yaml")

# Set missing secrets
App(app_id="my-app", secret_key="OPENAI_API_KEY", secret_value="sk-...")

# Inspect
App(app_id="my-app")
App(app_id="my-app", search_tools="read")
App(app_id="my-app", get_tool="Write")
App(app_id="my-app", security_profile=True)

# Undeploy
App(app_id="my-app", undeploy=True)
```

## `Chat` - sessions, queue, approvals, workspace, live events

Visible params:

| Param | Description |
|-------|-------------|
| `app_id` | App ID (required for first message). |
| `message` | Message to send. |
| `workspace` | Workspace directory path (passed as session metadata). |

Hidden params (selection):

- **Session control** - `session_id`, `watch`, `inspect`,
  `abort`, `resume`, `fork`, `compact`.
- **Memory + state** - `memory`, `tasks`, `history`.
- **Workspace** - `get_workspace`, `preview_snapshot`,
  `code_snapshot`, `file_path`, `approve_file`,
  `reject_file`.
- **Queue** - `queue`, `clear_queue`, `cancel_entry_id`.
- **Approvals + AskUser** - `pending`, `respond`,
  `approve_id`, `deny_id`.
- **Discovery** - `list_sessions`, `search`.

`watch=true` (recommended for testing) - non-blocking, returns
early on blockers (`pending_approval`, `pending_ask_user`,
`error`, `timeout`):

```python
Chat(app_id="my-app", message="Refactor the auth module", watch=True)
# → {session_id, status, text, tool_calls, timeline}
```

Multi-turn debug:

```python
Chat(app_id="my-app", message="List files in src/")     # → session_id
Chat(session_id="s123", message="Edit main.ts to add logging")
Chat(session_id="s123", inspect=True)                    # → tools_used, files_read, files_edited, behavior_violations
Chat(session_id="s123", memory=True)                     # → goal, facts, tasks
```

## `Run` - one-shot, pipeline, triggers, background

Visible params:

| Param | Description |
|-------|-------------|
| `app_id` | App ID (required). |
| `input_text` | Input for one-shot apps. |

Hidden params (selection):

- **Pipeline** - `pipeline`, `pipeline_input`.
- **Triggers** - `trigger_id`, `test_trigger`,
  `trigger_payload`.
- **Background sessions** - `background_message`,
  `background_payload`, `list_bg_sessions`,
  `bg_session_id`, `bg_pause_id`, `bg_resume_id`.
- **Background tasks** - `create_bg_task`,
  `list_bg_tasks`, `bg_task_id`, `wait_bg_task`,
  `cancel_bg_task_id`.
- **Watchers + activations** - `list_watchers`,
  `create_watcher`, `activations`, `errors`.

```python
# One-shot
Run(app_id="research", input_text="Compare React vs Vue for 2025")

# Pipeline with structured input
Run(app_id="etl", pipeline=True, pipeline_input={"urls": ["https://..."]})

# Fire a trigger manually
Run(app_id="notifier", trigger_id="daily-report",
    trigger_payload={"date": "2026-04-26"})

# Create a background session
Run(app_id="monitor", background_message="Watch for anomalies in the last hour")

# Wait on a background task
Run(app_id="batch", bg_task_id="t_abc", wait_bg_task=True)
```

## Recommended testing flow

```python
# 1. Validate YAML
App(yaml_path="app.yaml", validate_only=True)

# 2. Deploy + check required_secrets
App(yaml_path="app.yaml")

# 3. Set missing secrets
App(app_id="my-app", secret_key="KEY", secret_value="value")

# 4. Smoke test in watch mode (non-blocking, early blocker detection)
Chat(app_id="my-app", message="<realistic task>", watch=True)

# 5. Multi-turn follow-up
Chat(session_id="...", message="<follow-up>")

# 6. Inspect - tools_used, used_bash_for_files, behavior_violations
Chat(session_id="...", inspect=True)
```

Rules:

- Always validate before deploying.
- Always check `required_secrets` in the deploy response.
- Use realistic messages - not `"test"`.
- Use `watch=true` for testing to avoid timeouts + catch
  blockers early.
- Always inspect after a test turn.

## YAML usage

```yaml
tools:
  modules:
    dev_tools: {}                    # no config needed

agents:
  - id: builder
    modules:
      - dev_tools                    # full access to App, Chat, Run
```

A builder agent uses this module to write YAML → deploy →
smoke test → read history → fix in a loop.

## Cross-references

- Live testing SDK (the `digitorn.testing` library that
  `dev_tools` wraps):
- Dev CLI for testing apps from a terminal:
  [Dev CLI](../../language/46-dev-cli.md)
