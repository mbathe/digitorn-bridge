# Rules Matrix - Documented Behaviors + Their Tests

This file is the **contract between documentation and code**. Each row is a
rule that appears somewhere in the docs (or CLAUDE.md). Each rule maps to:

- **Category** - how it can be proven:
  - `S` (static) - proven by the static validator (`tools/validate_docs.py`)
  - `D` (deploy) - proven by the runtime smoke test (`tools/smoke_test_runtime.py`)
  - `B` (behavior) - needs a dedicated behavior test (`tools/behavior_tests.py`)
  - `MAN` (manual) - not automatable cheaply; relies on code review
- **Status** - `✅` passes, `❌` fails, `⏭` not yet implemented

_The goal: every rule is `✅` in its chosen category. When you add a rule to
the docs, you add a row here and a test to match._

## 1. Module counts & actions

| # | Rule | Source | Cat | Status |
|---|------|--------|-----|--------|
| M01 | `filesystem` exposes exactly 5 actions (read, write, edit, glob, grep) | `filesystem.md` | S | ✅ |
| M02 | `shell` exposes exactly 1 action `bash` with 5 modes | `shell.md` | S | ✅ |
| M03 | `agent_spawn` exposes exactly 1 action `agent` with 8 modes | `agent_spawn.md` | S | ✅ |
| M04 | `memory` exposes exactly 4 actions (remember, set_goal, task_create, task_update) | `memory.md` | S | ✅ |
| M05 | `context_builder` exposes 17 actions (5 discovery + 2 bg + 7 watcher + 3 other) | `context_builder.md` | S | ✅ |
| M06 | `database` exposes 16 actions | `database.md` | S | ✅ |
| M07 | `http` exposes 16 actions | `http.md` | S | ✅ |
| M08 | `web` exposes 4 actions (search, fetch, extract, download) | `web.md` | S | ✅ |
| M09 | `workspace` exposes 6 actions (write, read, edit, glob, grep, delete) | `workspace.md` | S | ✅ |
| M10 | `preview` has 17 actions, ALL `internal=True` | `preview.md` | S | ✅ |
| M11 | `widget` exposes 7 actions | `widget.md` | S | ✅ |
| M12 | `lsp` exposes 5 internal actions (diagnostics, check, notify_change, request, cancel_request) | `lsp.md` | S | ✅ |
| M13 | `cron_native` exposes 3 actions (schedule, cancel_schedule, remind) | `cron_native.md` | S | ✅ |
| M14 | `channels` exposes 11 actions | `channels.md` | S | ✅ |
| M15 | `vector` exposes 14 actions | `vector.md` | S | ✅ |
| M16 | `rag` exposes 14 actions | `rag.md` | S | ✅ |
| M17 | `queue` exposes 13 actions | `queue.md` | S | ✅ |
| M18 | `index` exposes 7 actions | `index_module.md` | S | ✅ |
| M19 | `llm_provider` exposes 6 actions | `llm_provider.md` | S | ✅ |
| M20 | `mcp` exposes 11 actions | `mcp.md` | S | ✅ |

## 2. Schema & YAML rules

| # | Rule | Source | Cat | Status |
|---|------|--------|-----|--------|
| Y01 | Every full app YAML in docs compiles cleanly | CLAUDE.md | S | ✅ |
| Y02 | `app.name` is required | schema.py | S | ✅ |
| Y03 | At least one agent required (except pipeline mode) | schema.py | S | ✅ |
| Y04 | `brain` is required on agents (unless reference mode) | schema.py | S | ✅ |
| Y05 | `connection_id` required on `database.connect` | database.md | S | ✅ |
| Y06 | `capabilities.grant` accepts only `{module, actions?, reason?}` objects | schema.py | S | ✅ |
| Y07 | Module config must be under `config:` wrapper (else silently dropped) | CLAUDE.md | B | ⏭ |
| Y08 | `brain.fallback` is an optional nested `AgentBrain` | schema.py | S | ✅ |
| Y09 | `triggers[].id` is required | schema.py | S | ✅ |
| Y10 | Background mode requires triggers OR channels module | schema.py | S | ✅ |
| Y11 | MCP servers must declare `sandbox` when app has capabilities profile | schema.py | S | ✅ |
| Y12 | `preview` top-level block requires `command` + `port` when present | schema.py | S | ✅ |
| Y13 | `runtime.mode` must be `conversation \| background \| one_shot \| pipeline` | schema.py | S | ✅ |
| Y14 | `brain.backend` must be `openai_compat`, `anthropic`, or `github_copilot` | schema.py | S | ✅ |

## 3. Deployment & lifecycle

| # | Rule | Source | Cat | Status |
|---|------|--------|-----|--------|
| L01 | Conversation app deploys cleanly | examples | D | ✅ |
| L02 | Multi-agent app (coordinator + specialist) deploys cleanly | 12-multi-agent | D | ✅ |
| L03 | Background app with cron trigger deploys cleanly | 09-triggers | D | ✅ |
| L04 | Channels app with webhook provider deploys cleanly | 40-channels | D | ✅ |
| L05 | RAG app deploys cleanly | 37-rag | D | ✅ |
| L06 | Workspace app with preview deploys cleanly | workspace.md | D | ✅ |
| L07 | Undeploy removes bundle + manifest | apps.py | D | ✅ |
| L08 | Deploy of an existing app_id without `force` fails gracefully | apps.py | B | ✅ |
| L09 | Deploy with `force=true` overwrites existing bundle | apps.py | B | ✅ (fixed - the upload endpoint's `force: bool = False` was defaulting to a query param so the multipart form value was being ignored; adding `Form(False)` fixed it) |
| TEN01 | Two users can install the same `app_id`; rows are distinct in DB (composite `(app_id, scope, owner_user_id)`) | models.py / manager.py | B | ✅ |
| TEN02 | Admin delete `?scope=system` removes only the system install; user installs survive | manager.delete_app | B | ✅ |
| TEN03 | Disabled user install is hidden from default list but visible to admin via `?include_disabled=true` | manager.list_disabled_apps | B | ✅ |
| L10 | App metadata (icon, color, category, quick_prompts) surfaces in `/api/apps` | apps.py | B | ✅ |

## 4. Session lifecycle

| # | Rule | Source | Cat | Status |
|---|------|--------|-----|--------|
| S01 | `POST /api/apps/{id}/sessions` creates a session and returns its id | REST_API.md | B | ⏭ |
| S02 | `GET /api/apps/{id}/sessions` lists existing sessions | REST_API.md | B | ⏭ |
| S03 | `DELETE /api/apps/{id}/sessions/{sid}` removes the session | REST_API.md | B | ⏭ |
| S04 | `GET /api/apps/{id}/sessions/{sid}/history` returns `{messages, events, preview_snapshot, memory_snapshot}` | REST_API.md | B | ⏭ |
| S05 | Session workspace path is isolated per-session when `sync_path` not set | CLAUDE.md | B | ⏭ |
| S06 | `POST /abort` returns 2xx and marks the session as interrupted | CLAUDE.md | B | ⏭ |
| S07 | `POST /fork` creates a new session from an existing one | apps.py | B | ⏭ |
| S08 | `session.idle_ttl: 0` disables idle expiry (permanent sessions) | configuration.md | MAN | ⏭ |
| WSP01 | Workspace snapshot persists across daemon restart via `GET /workspace` | PREVIEW.md | B | ✅ |
| WSP02 | Snapshot survives session close + reopen via REST | PREVIEW.md | B | ✅ |
| WSP03 | Debounced persist coalesces bursts into a single DB update (~500 ms window) | PREVIEW.md | B | ✅ |
| WSP04 | `cleanup_session` force-flushes before dropping in-memory state | preview/module.py | B | ✅ |
| WSP05 | `GET /workspace/export` returns portable `WorkspaceSnapshotEnvelope` | apps.py | B | ✅ |
| WSP06 | `POST /workspace/fork` creates a new session with the same workspace | apps.py | B | ✅ |
| WSP07 | `POST /workspace/import` with `replace=True` wipes + loads the envelope | apps.py | B | ✅ |
| TRX01 | `POST /api/transcribe` returns `{success, data.text, data.language}` for valid speech | voice_transcription.md | B | ✅ |
| TRX02 | Audio < 500 bytes → 422 | voice_transcription.md | B | ✅ |
| TRX03 | Audio > 25 MB → 413 | voice_transcription.md | B | ✅ |
| TRX04 | `GET /api/transcribe/health` reports provider + ready state | voice_transcription.md | B | ✅ |
| PIPE01 | Tool-chaining primitives: `_walk_path` + `_render_tool_templates` + `pipe` action registered | tool_chaining.md | B | ✅ |
| LSP01 | `lsp.diagnostics`, `lsp.check`, `lsp.notify_change` are `internal=True` (hidden from LLM) | hooks.md | B | ✅ |
| HK03 | Hook schema: `max_fires`, `priority`, `enabled`, `tags`, `agent_id` honored at runtime | hooks.md | B | ✅ |
| HK04 | Composite conditions `all_of` / `any_of` / `not` / `never` registered with short-circuit | hooks.md | B | ✅ |
| HK05 | Event aliases `pre_tool_use` / `post_tool_use` / `user_prompt` resolve to canonical events | hooks.md | B | ✅ |
| HK06 | Per-agent hook scope via `agents[].hooks[]` with `agent_id` filter | hooks.md | B | ✅ |

## 5. Security & capabilities

| # | Rule | Source | Cat | Status |
|---|------|--------|-----|--------|
| SEC01 | `capabilities.deny` blocks the action for the LLM schema | 11-security | B | ⏭ |
| SEC02 | `capabilities.grant` with empty `actions` grants all actions of that module | 11-security | B | ⏭ |
| SEC03 | `capabilities.approve` makes the action require a user approval | 11-security | B | ⏭ |
| SEC04 | No loopback bypass: `RemoteAuthMiddleware` requires a Bearer token on every `/api/*` path even from 127.0.0.1 (only `/health`, `/healthz`, `/.well-known/*`, `/docs`, `/redoc`, `/openapi.json`, `/auth/*` skip auth) | RemoteAuthMiddleware (digitorn_auth) | B | ✅ |
| SEC05 | Non-loopback requests without JWT to /auth/me return 401 | RemoteAuthMiddleware (digitorn_auth) | B | ⏭ |
| SEC06 | Granular action filter in `agents[].modules: [{filesystem: [read]}]` restricts sub-agent tools | CLAUDE.md | B | ⏭ |

## 6. REST API surface

| # | Rule | Source | Cat | Status |
|---|------|--------|-----|--------|
| API01 | All documented routes exist in code (exact match) | REST_API.md | S | ✅ |
| API02 | `/health`, `/healthz`, `/readyz` return 200 | server.py | B | ✅ |
| API03 | `/api/metrics` returns JSON; `/api/metrics/prometheus` returns text | server.py | B | ⏭ |
| API04 | `/api/discovery/modules` returns a list of loaded modules | discovery.py | B | ⏭ |
| API05 | `/api/apps` returns all deployed apps | apps.py | B | ✅ |
| API06 | `/api/apps/{id}/reload` re-reads bundle without restart | apps.py | B | ⏭ |
| API07 | `/api/modules/{id}/health` returns status | modules.py | B | ⏭ |
| API08 | `/api/config` (GET) returns the effective config | config.py | B | ⏭ |

## 7. Socket.IO

| # | Rule | Source | Cat | Status |
|---|------|--------|-----|--------|
| SIO01 | Socket.IO namespace `/events` accepts JWT in URL query param | SOCKETIO.md | MAN | ⏭ |
| SIO02 | Transports are restricted to `["websocket"]` (polling rejected) | SOCKETIO.md | MAN | ⏭ |
| SIO03 | All documented event types (`_EVENT_KIND_MAP`) match code | SOCKETIO.md | S | ✅ |

## 8. Modules - behavior contracts

| # | Rule | Source | Cat | Status |
|---|------|--------|-----|--------|
| FS01 | `Edit` on a large (>500 b) un-Read file fails with a clear error | CLAUDE.md | B | ⏭ |
| FS02 | `Write` followed immediately by `Edit` succeeds (write adds path to read-set) | CLAUDE.md | B | ⏭ |
| FS03 | Small files (<500 b) can be edited without prior `Read` | CLAUDE.md | B | ⏭ |
| FS04 | `Write` (filesystem) does NOT include `lint` - lint is a `workspace` feature only | code | S | ✅ |
| FS05 | Relative paths in `filesystem.*` resolve from `self.workspace` | CLAUDE.md | MAN | ⏭ |
| WS01 | `WsWrite` publishes a `preview:resource_set` Socket.IO event | workspace.md | B | ⏭ |
| WS02 | `sync_to_disk: true` mirrors writes to the real filesystem | workspace.md | B | ⏭ |
| WS03 | `lint: true` runs built-in validators (JSON, YAML, TOML, Python) | workspace.md | B | ✅ |
| MEM01 | `Remember` content appears in next turn's memory block | memory.md | B | ⏭ |
| MEM02 | Sensitive env var values are redacted before being stored | memory.md | B | ⏭ |
| SH01 | `Bash` sync mode returns stdout/stderr/exit_code | shell.md | B | ⏭ |
| SH02 | `Bash(run_in_background=true)` returns `task_id` immediately | shell.md | B | ⏭ |
| SH03 | `Bash(task_id=..., kill=true)` terminates a running task | shell.md | B | ⏭ |
| AG01 | `Agent(prompt=...)` returns an `agent_id` immediately (background) | agent_spawn.md | B | ⏭ |
| AG02 | `Agent(prompt=..., wait=true)` blocks until completion | agent_spawn.md | B | ⏭ |
| CRON01 | `cron_native.schedule(when="in 5s")` fires after ~5 s | cron_native.md | B | ⏭ |
| RAG01 | `create_knowledge_base` + `ingest` + `query` round-trip works | rag.md | B | ⏭ |
| WID01 | `widget.render(ref=...)` emits a `widget:render` Socket.IO event | widget.md | B | ⏭ |

## 9. Brain / LLM provider

| # | Rule | Source | Cat | Status |
|---|------|--------|-----|--------|
| BR01 | `api_key: "claude-code"` reads token from `~/.claude/.credentials.json` | CLAUDE.md | B | ⏭ |
| BR02 | `brain.fallback` kicks in on 402/credit error | CLAUDE.md | B | ⏭ |
| BR03 | Next turn after a fallback tries primary again | CLAUDE.md | MAN | ⏭ |
| BR04 | Auto-coerce converts `"40"` to `40` for int params | CLAUDE.md | B | ⏭ |
| BR05 | Input-JSON-delta recovery reconstructs truncated tool params | CLAUDE.md | MAN | ⏭ |

## 10. Hooks V2

| # | Rule | Source | Cat | Status |
|---|------|--------|-----|--------|
| HK01 | 14 hook conditions exist (always, never, context_pressure, turn_count, tool_calls, message_count, tool_name, tool_failed, content_contains, error_type, expression, all_of, any_of, not) | hooks.md | S | ✅ |
| HK02 | 13 general hook actions + 5 builder-specific exist | hooks.md | S | ✅ |
| HK03 | Hook `type: gate` blocks tool execution when predicate true | hooks.md | B | ⏭ |
| HK04 | Hook `type: transform_params` modifies params before execution | hooks.md | B | ⏭ |
| HK05 | Hook `type: transform_result` modifies results after execution | hooks.md | B | ⏭ |
| HK06 | Hook `type: chain` runs multiple actions sequentially | hooks.md | B | ⏭ |

## 11. Channels module

| # | Rule | Source | Cat | Status |
|---|------|--------|-----|--------|
| CH01 | Webhook provider accepts POST at its `inbound_path` | channels.md | B | ⏭ |
| CH02 | Filter drops events that don't match | channels.md | B | ⏭ |
| CH03 | Session strategy `per_event` creates a fresh session per inbound event | channels.md | B | ⏭ |
| CH04 | Route rule picks agent by field match | channels.md | MAN | ⏭ |
| CH05 | `reply` uses the stored `_channel_reply_context` | channels.md | MAN | ⏭ |

## 12. Configuration

| # | Rule | Source | Cat | Status |
|---|------|--------|-----|--------|
| CFG01 | 17 config sections exist | configuration.md | S | ✅ |
| CFG02 | Env vars with `DIGITORN_` prefix override config | configuration.md | MAN | ⏭ |
| CFG03 | Nested env vars use `__` separator (e.g. `DIGITORN_SERVER__PORT`) | configuration.md | MAN | ⏭ |
| CFG04 | Priority: env > user > system > defaults | configuration.md | MAN | ⏭ |

---

## Rollup

| Category | Covered | Pending |
|----------|---------|---------|
| S (static)    | many   | 0 (ran via `validate_docs.py`) |
| D (deploy)    | many   | 0 (ran via `smoke_test_runtime.py`) |
| B (behavior)  | 2   | ~40 (implement in `behavior_tests.py`) |
| MAN (manual)  | 0   | ~15 (not automatable) |

**Next**: `tools/behavior_tests.py` will target every `B` row. Each test sets up
a minimal app, exercises the behavior, asserts the documented outcome, and
tears down. When a test is added, the matrix row flips to ✅.
