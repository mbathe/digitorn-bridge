# dev_tools — actions

3 tools, each a multi-mode dispatcher. Pass the flags that match your intent.

---

## `App` — app lifecycle, discovery, packages, MCP, drafts, security

### Visible params
| Name | Type | Purpose |
|---|---|---|
| `yaml_path` | str | Path to app YAML (for deploy/validate) |
| `app_id` | str | App ID (for status/undeploy/secrets/tools) |

### Modes (hidden flags)
- **Validate**: `yaml_path`, `validate_only=true`
- **Deploy (file)**: `yaml_path`
- **Deploy (inline)**: `yaml_content="..."`
- **Status**: `app_id`
- **Undeploy**: `app_id`, `undeploy=true`
- **List**: `list_apps=true`
- **Secrets**: `app_id`, `secret_key`, `secret_value`
- **User credentials**: `credential_provider`, `credential_fields`, `list_credentials`, `delete_credential_id`
- **Tools discovery**: `app_id`, `search_tools` or `get_tool`
- **Compile (builder)**: `yaml_content`, `compile_yaml=true`
- **Prompt preview**: `yaml_content`, `prompt_preview=true`, `agent_id`
- **Manifest gen**: `yaml_content`, `generate_manifest=true`
- **Drafts**: `list_drafts`, `create_draft_yaml`+`draft_name`, `update_draft_id`+`yaml_content`, `deploy_draft_id`, `delete_draft_id`
- **Packages**: `list_packages`, `package_source`, `uninstall_package`, `upgrade_package`
- **MCP**: `mcp_catalog`, `mcp_list`, `mcp_install`, `mcp_delete_id`, `mcp_test_id`
- **Security**: `app_id`, `security_profile=true`
- **Diagnostics**: `app_id`, `diagnostics=true`
- **Health**: `health=true`
- **Discovery**: `list_modules`, `list_templates`, `list_triggers`

---

## `Chat` — sessions, approvals, memory, workspace, live events

### Visible params
| Name | Type | Purpose |
|---|---|---|
| `app_id` | str | App ID (required for first message) |
| `message` | str | Message to send |
| `workspace` | str | Workspace directory path |

### Modes (hidden flags)

**Send**
- `app_id`, `message` — new session
- `session_id`, `message` — follow-up
- `queue_mode` = `"async"` | `"wait"` | `"replace_last"` — send while a turn is running
- `client_message_id` — idempotency key
- `image_paths=[...]` — attach images (paths; encoded automatically)
- `watch=true` — **live-stream the turn, return early on blockers**, return a seq-ordered timeline. Strongly recommended for testing.
- `watch_include_tokens=true` — include per-token events (verbose)
- `watch_max_events=N` — cap timeline length

**Inspect (session_id)**
- `inspect=true` — turns + tools + violations
- `memory=true` — goal, todos, facts
- `tasks=true` — task list
- `history=true` — full message history
- `persistent_events=true`, `since_seq=N` — durable event log
- `context_breakdown=true` — token breakdown per source

**Workspace / preview (session_id)**
- `get_workspace=true` — metadata
- `preview_snapshot=true` — UI state
- `code_snapshot=true` — file tree (no content)
- `file_path=...` — specific file
- `approve_file=...` / `reject_file=...`

**Queue (session_id)**
- `queue=true` — list
- `clear_queue=true` — cancel all queued
- `cancel_entry_id=...` — cancel one

**Control (session_id)**
- `abort=true`, `purge_queue_on_abort=true`
- `resume=true` — after crash/interrupt
- `fork=true` / `compact=true` / `export_session=true` / `delete_session=true`

**Approvals (session_id)**
- `pending=true` — what's blocking
- `respond="my answer"` — answer ask_user
- `approve_id=<rid>` / `deny_id=<rid>`

**Find sessions**
- `app_id`, `list_sessions=true`
- `app_id`, `search="<query>"`

### `watch` return shape
```
{
  "session_id": "...",
  "correlation_id": "...",
  "status": "completed" | "pending_approval" | "pending_ask_user" | "error" | "timeout",
  "text": "<assistant text, up to 4000 chars>",
  "tool_calls": [{"name", "params", "result_preview"}],
  "pending_approvals": [...],
  "timeline": [{"type", "seq", ...}, ...],
  "event_count": N,
  "last_seq": N
}
```

---

## `Run` — non-conversational execution

### Visible params
| Name | Type | Purpose |
|---|---|---|
| `app_id` | str | App ID (required) |
| `input_text` | str | Input for one-shot apps |

### Modes (hidden flags)

- **One-shot**: `app_id`, `input_text`
- **Pipeline**: `pipeline=true`, `pipeline_input={...}`
- **Triggers**: `trigger_id`, `trigger_payload`, optional `test_trigger=true`
- **Background sessions**: `background_message`, `background_payload`, `list_bg_sessions`, `bg_session_id`, `bg_pause_id`, `bg_resume_id`
- **Background tasks**: `create_bg_task={...}`, `list_bg_tasks`, `bg_task_id`, `wait_bg_task`, `cancel_bg_task_id`
- **Watchers**: `list_watchers`, `create_watcher={...}`
- **Activations / errors**: `activations=true`, `errors=true`

---

## Rules for the agent

1. **Always validate before deploying** — `App(yaml_path=..., validate_only=true)` first.
2. **Always check required_secrets** after deploy — the app won't work without them.
3. **Prefer `Chat(watch=true)` for tests** — returns early on blockers, no wasted timeout.
4. **Realistic test messages** — "analyze this project", not "test".
5. **Multi-turn every test** — 2-3 turns minimum to prove memory works.
6. **Inspect after testing** — `Chat(session_id=..., inspect=true)` to check tools_used and violations.
7. **Never mock** — this SDK hits the real daemon + real LLM.
