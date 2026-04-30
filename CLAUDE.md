# Digitorn Bridge - Context for Claude

## What This Project Is
Declarative AI agent framework in Python. Build agent apps with YAML. The main test app is `examples/opencode/app.yaml` - a Claude Code equivalent.

## Critical Architecture Decisions

### Tool Names (tool_names.py)
- All tools use SHORT names like Claude Code: Write, Read, Edit, Bash, Grep, Glob, Agent, Remember, TodoAdd, etc.
- Centralized in `packages/digitorn/core/runtime/tool_names.py` - single source of truth
- `to_fqn("Write")` → `"filesystem.write"`, `to_short("filesystem.write")` → `"Write"`
- Auto-generates PascalCase names for modules without explicit mapping
- `context_builder.remember` uses `SetReminder` to avoid collision with `memory.remember` (`Remember`)

### Hidden Params (tool_schema.py)
- Params with `json_schema_extra={"hidden": True}` are excluded from the JSON schema sent to the LLM
- The LLM sees only essential params (e.g. Write: path + content only)
- `action_entry_to_json_schema()` in `context_builder/tool_schema.py` filters them out
- `additionalProperties: false` and `strict: true` added in `build_direct_tools()`

### Tool Prompts (decorator + prompt injection)
- Each `@action` has a `tool_prompt=""` field for detailed LLM instructions
- These are injected into the system prompt by `context_builder/prompt.py` under `# Tool Usage Instructions`
- The `description` field stays SHORT (1 line) - only for the tool schema

### Shell on Windows
- Uses **Git Bash** (NOT PowerShell, NOT WSL)
- `platform_adapters.py` WindowsAdapter.default_shell searches Git Bash by explicit path
- NEVER use `shutil.which("bash")` - it returns WSL bash which crashes
- All bash syntax works natively: &&, |, grep, cat, 2>&1, head, tail
- PowerShell conversion code was REMOVED - not needed with Git Bash

### Auth - Claude Code OAuth Token
- Provider in `llm_provider/providers/anthropic.py` supports `api_key: "claude-code"`
- Reads token from `~/.claude/.credentials.json` (claudeAiOauth.accessToken)
- Sends headers: `x-app: cli`, `anthropic-beta: oauth-2025-04-20,claude-code-20250219`
- Has 15 retries with exponential backoff for rate limits
- Token cached in memory with expiry check

### Sub-agent Architecture (agent_spawn/)

**Module sharing**: `memory`, `web`, `lsp`, `filesystem`, `shell` modules are SHARED with sub-agents (same instance). Other modules get fresh instances. This ensures sub-agents have the same workspace, cwd, read_files set, and memory store.

**1 tool, 8 modes**: The Agent tool dispatches via params:
- Mode 1: `Agent(prompt='...')` - background (default), returns agent_id
- Mode 2: `Agent(prompt='...', wait=true)` - blocking, returns result
- Mode 3: `Agent(agent_id='...')` - check status
- Mode 4: `Agent(agent_id='...', wait=true)` - wait for one
- Mode 5: `Agent(agent_ids=[...])` - wait for multiple
- Mode 6: `Agent(agent_id='...', cancel=true)` - cancel
- Mode 7: `Agent(agent_id='...', reassign='new task')` - respawn
- Mode 8: `Agent(list=true)` - list all

**Background by default**: `wait=false` is the default. The LLM sees the `wait` param in the schema and can set `wait=true` when it needs blocking. Multiple `Agent()` calls in one turn run concurrently via `asyncio.gather` (agent is in `_READ_ONLY_ACTIONS`).

**Universal directives**: `runner.py` injects a mandatory prefix before every specialist's system prompt: "Be FAST, no filler, go straight to tool calls, return only key findings." Sub-agents never create tasks or set goals.

**Granular tool restriction** (YAML):
```yaml
agents:
  - id: explore
    modules:
      - {filesystem: [read, grep, glob]}  # only these 3 actions
      - {shell: [bash]}                    # full module
      - {memory: [remember]}               # single action
  - id: worker
    modules: [filesystem, shell, memory]   # simple format = full access
  - id: writer
    modules:
      - {memory: [remember]}               # writer has NO external tools
```
Parsed in `bootstrap.py::_register_specialist()` → `action_filter` dict → passed to `build_index(action_filter=...)` → `_index_module(only_actions=set(...))`. The LLM schema contains ONLY the allowed tools. Tested: explore gets 5 tools, web_researcher gets 3, writer gets 1.

**Abort cleanup**: `POST /sessions/{sid}/abort` kills:
1. The agent turn (asyncio task cancel)
2. Background shell tasks (`shell.cleanup_session`)
3. Running sub-agents (`agent_spawn.cleanup_session`) - emits `agent_cancel` events
4. Context builder bg tasks (`context_builder.cleanup_session_bg_tasks`)
On resume, orphaned tool_calls get synthetic `"interrupted": true` results.

**Agent events for frontend**: Emitted via `_notify_bg` → `_relay` in manager.py → `agent_event` SSE:
- `spawn_agent` - agent launched (agent_id, specialist, task)
- `agent_progress` - running (duration_seconds, tool_calls_count, preview)
- `agent_result` - completed/failed (result_summary, error)
- `agent_cancel` - cancelled (reason, duration_seconds)

### Filesystem Guards & Path Resolution
- `write()` adds path to `_read_files` after writing (so subsequent `edit()` works without reading)
- Small files (<500 bytes) can be edited without prior read
- Large files (>500 bytes) require read first (protects against data loss)
- `_resolve_path(file_path)` resolves relative paths from `self.workspace` (not CWD)
- Directory detection: `os.path.isdir()` check before `open()` - Windows returns `PermissionError` on dirs
- Workspace module: `_resolve_ws_path()` converts absolute paths to workspace-relative (strips sync_dir prefix)
- Shell module: Git Bash paths (`/c/Users/...`) converted to Windows (`C:/Users/...`) before workspace check
- Shell allowed roots: workspace + user home dir + temp dir (always allowed)

### Brain Fallback (billing failover)
- `AgentBrain.fallback` in schema.py - optional nested brain config
- On 402 / "Insufficient Balance" / "credit" errors, `_handle_llm_error` in agent_loop.py switches to `ctx._fallback_brain`
- Wired in bootstrap.py after `AgentContext` creation from `agent.brain.fallback`
- Temporary: next turn retries primary provider first
- YAML: `brain: { ..., fallback: { provider: anthropic, model: claude-haiku-4-5, config: { api_key: "claude-code" } } }`

### Streaming JSON Recovery (anthropic provider)
- `input_json_delta` events are accumulated during streaming
- If SDK's `get_final_message()` returns empty tool input, we reconstruct from accumulated fragments
- `_recover_tool_json()` handles truncated JSON (closes braces, regex extraction)
- `max_tokens: 16384` in YAML to avoid truncation

### Auto-coerce Params (base.py)
- `_auto_coerce_params()` runs before Pydantic validation
- Converts: string→int ("40"→40), string→bool ("true"→True), string→float
- Maps wrong param names to closest required params

### CLI TUI Architecture
- `app.py` - main TUI app, keybindings, event handlers, slash menu
- `chat_log.py` - message display, tool calls, thinking, diffs
- `sidebar.py` - workspace panel (goal, todos, facts, agents, git)
- `status_footer.py` - bottom bar with tokens, context pressure
- `backends/standalone.py` - in-process agent execution
- `backends/daemon.py` - HTTP daemon SSE streaming

### Silent Tools (not shown in chat)
- Memory tools: SetGoal, Remember, Recall, Forget, TodoAdd, TodoUpdate
- Agent tools: Agent, AgentWait, AgentWaitAll, AgentResult, AgentStatus, AgentCancel, AgentList
- Discovery meta-tools: SearchTools, GetTool, ListCategories, BrowseCategory
- These are tracked in `_SILENT_TOOLS` set in `app.py`

### AskUser Action
- In `context_builder/actions_meta.py` - uses ApprovalQueue
- Exposed via `capabilities.grant: [{module: context_builder, actions: [ask_user]}]`
- `compiler.py` preserves action_overrides for hidden system modules
- `builder.py` indexes explicitly granted actions from hidden modules

### Hooks V2 System (hooks.py)
- 15 events: turn_start ✅, turn_end ✅, tool_start ✅, tool_end ✅, session_start ✅ (turn==0), session_end ✅ (manager.end_session), pre_compact ✅, error ✅, approval_request ✅ (ApprovalQueue.enqueue callback), agent_spawn ✅ / agent_complete ✅ (agent_spawn module), + aliases pre_tool_use→tool_start, post_tool_use→tool_end, user_prompt→turn_start. Only `activation` remains declared-only (background-trigger routing).
- 14 conditions: always, never, context_pressure, turn_count, tool_calls, message_count, tool_name, tool_failed, content_contains, error_type, expression + composite `all_of` / `any_of` / `not` (short-circuit, nest freely).
- 13 actions: compact_context, inject_message, module_action, module_action_inject, log, shell, gate, transform_params, transform_result, chain, notify, lsp_diagnose, pipe
- Hook schema: `id`, `on`, `condition`, `action`, `cooldown`, `max_fires` (0=unlimited), `priority` (lower=earlier, default 100), `enabled` (feature flag), `tags` (list[str]). Scopes: `execution.hooks[]` (app-wide) + `agents[].hooks[]` (per-agent - each stamped with `agent_id`, runtime filter fires only for matching agent's turns).
- `lsp_diagnose`: universal post-write LSP trigger. Reads `{{tool.params.path|file_path}}` + `{{tool.params.content}}` from any write-like tool, calls `lsp.notify_change`, publishes results to the `diagnostics` preview channel. Lets any module (filesystem, workspace, custom writers, MCP tools) get free diagnostics via one YAML hook. Flags: `inject_result: true` merges lint into the tool's response (self-correction loop); `read_from_disk: true` reads content from disk when absent from params; `publish: true` pushes to preview channel for UI.
- `pipe`: generic tool-chaining primitive. Routes the output of the current tool into any other tool (native module or MCP) with field extraction. YAML: `{type: pipe, to: target.tool, map: {param: "{{tool.result.nested.field}}"}, extra: {literal_flag: true}, on_error: ignore|log|raise}`. Works with the full `{{tool.*}}` placeholder syntax - supports dotted paths, array indices (`items.0.id`), whole-result JSON via `{{tool.result}}`.
- Templating primitives in `hooks.py`: `_walk_path(obj, "a.b.0.c")` for jsonpath-lite navigation, `_render_tool_templates(value, state)` for recursive template resolution. Applied automatically by `module_action`, `module_action_inject`, `pipe`, and `shell`. Placeholders: `{{tool.name}}`, `{{tool.params.X}}`, `{{tool.result.X}}`, `{{tool.error}}`, `{{tool.result}}` (whole JSON).
- Power features: gate blocks tool execution, transform modifies params/results, chain runs multiple actions, shell runs commands with {{tool.*}} templates
- Condition/action registries are extensible via `@register_condition` and `@register_action`

### Config System (config.py)
- 67 params across 14 sections: server, database, auth, session, runtime, agent_spawn, mcp, sandbox, websocket, default_model, discovery, modules, app, logging
- Loaded from: defaults < /etc/digitorn/config.yaml < ~/.digitorn/config.yaml < env vars (DIGITORN_ prefix)
- Pydantic BaseSettings with nested models, env_nested_delimiter="__"
- Singleton via `get_settings()`, overridable via `override_settings()` for tests

### Daemon API Routes (removed)
- `/chat` and `/chat/stream` routes REMOVED - only `/sessions/{sid}/messages` now
- All communication is session-based (create session, send messages, get SSE stream)

### Built-in App (digitorn-chat)
- Auto-deployed at daemon startup using `default_model` config values
- `app_id: "digitorn-chat"` - generic conversational agent
- Injected values: provider, model, backend, api_key, base_url, temperature, max_tokens, context_window

### App Metadata
- `app.icon` - emoji or URL for the app icon
- `app.color` - hex color for the app card
- `app.category` - app category (e.g. "development", "data", "assistant")
- `app.quick_prompts` - list of suggested prompts shown to users

### Fuzzy Edit Matching (filesystem/helpers.py)
- 6 strategies in cascade: exact → per-line trailing whitespace → CRLF → whitespace collapse → indentation-agnostic (strip both sides) → fuzzy block (85% SequenceMatcher)
- All strategies return positions in the ORIGINAL content (not normalized)
- `_reindent_replacement(old, new, matched)` auto-adjusts indentation: if the LLM sent 0-indent old_string but the file has 4-indent, new_string is re-indented to match
- On failure: `find_closest_matches()` suggests up to 3 matches (>50% similarity) with line numbers
- Used by both `filesystem.edit` and `workspace.edit` (shared helpers)

### Built-in Validators (lsp/parsers.py)
- JSON (.json, .jsonc), YAML (.yaml, .yml), TOML (.toml), Python (.py, .pyi)
- Run after every write/edit - lint results appear as `lint` field in tool output
- LSP module tried first (ruff, eslint, etc.), then built-in fallback

### Error Classification (_classify_error in api/apps.py)
- Classifies exceptions into structured dicts: {error, code, category, retry, detail}
- Categories: billing, auth, rate_limit, provider, network, internal
- Sent to clients via SSE for proper error handling and retry logic

### Abort Flow
- `POST /sessions/{sid}/abort` triggers full cleanup:
  1. `task.cancel()` on the agent turn → CancelledError → `session.interrupted = True`
  2. `shell.cleanup_session(sid)` → kills all background processes, emits cancelled notifications
  3. `agent_spawn.cleanup_session(sid)` → cancels all sub-agent asyncio tasks, emits `agent_cancel` events per agent
  4. `context_builder.cleanup_session_bg_tasks(sid)` → cancels watchers and background_run tasks
  5. Publishes `abort` SSE event → frontend shows "Interrupted"
- On resume: `_recover_interrupted_session()` injects synthetic `"interrupted": true` tool results for orphaned calls
- Clean shutdown: no dangling tasks, no stuck busy state, no orphaned sub-agents

### Behavior Engine (behavior/)
- Runtime enforcement module - monitors every tool call, detects violations, injects corrections
- **14 built-in rules**: read_before_edit, no_bash_for_files, confirm_destructive (BLOCKS), test_after_changes, verify_after_edit, search_before_read, delegate_complex, etc.
- **5 profiles**: coding, research, data, creative, assistant - sensible defaults per app type
- **Custom rules** in YAML: `condition` (contains/matches/not_in) + `action` (block/warn/remind) + `trigger` (tool name)
- **Semantic classifier** (optional): small LLM analyzes user message BEFORE the main agent acts, classifies task complexity, injects behavioral directives
- **3 enforcement levels**: `block` (tool prevented), `warn` (message injected), `remind` (post-tool hint)
- **Session-isolated state**: read_files, edited_files, reads_since_search, changes_since_test - per session, never cross-contaminate
- YAML: `behavior: { profile: coding, classify_turns: true, brain: { provider: anthropic, model: claude-haiku-4-5 }, rules: {...}, custom: [...] }`
- Files: `packages/digitorn/modules/behavior/` (module.py, engine.py, rules.py, classifier.py, profiles.py, state.py)
- Wired in `bootstrap.py::_wire_behavior_module()` → `ctx.behavior_module`
- Called in `agent_loop.py`: `classify_turn()` at turn 0, `pre_tool_check()` before each tool, `post_tool_check()` after each tool

### Dev CLI (cli/dev.py)
- `digitorn dev deploy|status|chat|history` - test apps against the real daemon
- `chat` mode: sends messages via POST `/sessions/{sid}/messages`, polls GET `/sessions/{sid}`, auto-approves pending approvals
- Single message mode: `digitorn dev chat app_id -m "message"` - non-interactive, for scripts/agents
- Auto-approval: polls `GET /approvals` every second and auto-resolves all pending requests
- Python API: `from digitorn.core.cli.dev import dev_cli; dev_cli(["chat", "app", "-m", "test"])` - used by Builder agent
- Test app: `app-test.yaml` with `default_policy: auto` (no approval popups) for dev testing
- Builder workflow: write YAML → deploy → smoke test → functional test → verify history → fix if needed

### File Tree with Insertions/Deletions
- `_track_file()` reports insertions/deletions per write/edit/rm operation
- Counts based on actual line diffs (not byte counts)
- Streamed to workbench via SSE for real-time file tree display
- Tracks: operation type, file size, insertion count, deletion count

### Background Trigger Routing
- 3 modes: broadcast (shared session), user (per-user session), session (per-key)
- `routing_key` templates: `{{event.header.X-User-Id}}`, `{{event.body}}`, etc.
- `max_concurrent_activations` (default: 20) throttles parallel activations
- HTTP triggers: aiohttp preferred, basic asyncio TCP fallback when unavailable

### Credentials System (core/credentials/)
- **Centralized vault**: every secret lives in the encrypted `credentials` table; apps reference by name in YAML. Full doc in `docs/credentials.md`.
- **YAML block**: `credential: { ref: openai_main, scope: per_user, provider: openai }` (or compact form `credential: openai_main`). Schema field on `AgentBrain` and `ModuleBlock`.
- **4 scopes**: `system_wide` / `per_app_shared` (resolved at deploy) + `per_user` / `per_app_per_user` (resolved at session start). Scope-strict lookup, no fallback cascade.
- **Two injection passes**: `inject_deploy_time.py` mutates `compiled.modules[mid].config` BEFORE `bootstrap()`; `inject_session_time.py` hot-swaps live LLM provider instances at session start (uses `_override_provider_fields` shared with the legacy `{{secret.X}}` path).
- **19 handlers** under `core/credentials/handlers/` (api_key, oauth2, multi_field, ssh_key, mTLS, …). Each has `schema_fields()` + `validate_fields()` + `test_live_connection()` + `refresh()` + `revoke()`.
- **TOML provider catalog** under `core/credentials/catalog/builtins/` (16 templates). Drop a TOML file + restart = new provider in the picker.
- **CredentialSlot** declared on consumer modules (`llm_provider`, `mcp`, `channels`). Compiler walks slots → manifest. Runtime injector reads `slot.inject` to write decrypted fields at the right config path.
- **Master key**: `DIGITORN_KMS=env|file|aws|gcp|azure|vault`. Default env reads `DIGITORN_MASTER_KEY` (32 bytes b64url). Production = KMS with envelope encryption (per-row wrapped DEK).
- **Audit log**: hash-chained `credential_audit` table. Verify with `POST /api/admin/credentials/audit/verify`.
- **OAuth flow**: 5 builtins (Notion/Google/GitHub/Slack/Discord) in `core/credentials/oauth_providers.py`. Background refresh loop in `oauth_refresh_loop.py` runs every 5 min, renews tokens within 10 min of expiry. Revocation in `handlers/oauth2.py::revoke`.
- **Backward compat**: legacy `{{secret.X}}` / `{{env.X}}` still resolved at runtime via `runtime_resolver.py`. Compiler warns when a YAML uses templates without `credential:`. Run `digitorn yaml migrate-credentials <file>` to auto-migrate.
- **Health endpoint**: `GET /api/credentials/health` returns master_key + cipher + audit + oauth_registry + refresh_loop state.

## Known Issues to Fix

### Fixed UX Bugs (2026-04-03)
1. **Agent completion** - ✅ auto-cleanup after 6s, daemon wait_all handled, agent_event SSE handled
2. **Todo/memory sidebar** - ✅ PascalCase short names (TodoAdd, SetGoal) now recognized
3. **Token counter** - ✅ reset on compaction, correct after context overflow
4. **Rate limit/connection retries** - ✅ spinner shows "Rate limited" or "Retrying" with attempt info
5. **Spinner states** - ✅ modes: requesting, generating, thinking, tool_use, rate_limited, waiting
6. **Crash guards** - ✅ try/except on all widget.remove() and timer.stop()
7. **Thinking in daemon** - ✅ progressive thinking_started/thinking_delta SSE events
8. **Generation guard** - ✅ generation counter prevents stale cleanup and stuck busy state
9. **Abort cleanup** - ✅ finalize streams, bump generation, show "Interrupted by user"
10. **Stall detection** - ✅ >8s without activity → spinner shows "Waiting"

### Added Features (2026-04-04)
11. **max_output_tokens recovery** - 3 auto-resume attempts when LLM hits token limit
12. **OAuth 401 token refresh** - auto-reload token from credentials.json on auth error
13. **Widget pruning** - max 300 widgets, oldest pruned to prevent memory bloat
14. **Streaming finish_reason** - _FakeResponse now carries finish_reason for recovery
15. **Input history** - Up/Down arrows navigate previous messages (100 entries max)
16. **18 slash commands** - /compact, /cost, /diff, /commit, /model, /context + 12 existing
17. **/commit** - sends commit instruction to agent with optional message hint
18. **/model** - shows provider, capabilities, context window, max output
19. **/context** - shows context window breakdown (system/tools/messages %)

### Things That Work
- Tool name resolution (all formats → FQN → short)
- Git Bash execution on Windows (&&, grep, pipes, 2>&1)
- Filesystem operations (read, write, edit with guards)
- Memory operations (goal, remember, recall, todos)
- OAuth token loading and API calls
- Slash menu with autocomplete
- Sidebar with command panels
- Short tool names in API (Write, Read, Edit, Bash...)
- Auto-coerce of mistyped params
- Tool prompt injection in system prompt
- run_parallel with short name resolution
- System prompt styled like Claude Code (concise, 65 lines)

### Multimodal / Image Support
- `ImageStore` in `core/image_store.py` - disk-backed, session-isolated, cleanup
- `multimodal.py` in `core/runtime/` - build_user_message_with_images, resolve_images_for_provider, inject_tool_image
- **Image Aging**: current turn = full resolution, 1-2 turns = low-res (512px), 3+ turns = text description only
- **Provider conversion**: Anthropic format `{type: "image", source: {base64}}` ↔ OpenAI format `{type: "image_url", image_url: {url}}`
- **Providers without vision** (DeepSeek-chat): images auto-converted to `[Image: description]` text
- **YAML config**: `brain.vision` (null=auto, true/false), `brain.image_generation`, `brain.image_detail` (auto/low/high), `brain.max_images_per_turn`
- **Auto-detection**: model name contains claude/gpt-4o/gemini/llava → vision=true
- **Daemon config**: `images.*` section (8 params: max_per_message, max_size_bytes, aging, storage_dir, etc.)
- **API**: POST `/messages` accepts `images: [{data, mime, name}]`, GET `/sessions/{sid}/images/{id}` serves raw bytes
- **SSE**: `tool_call` events include `image_data` + `image_mime` when tool result has an image
- **filesystem.read**: detects image files → returns metadata + `metadata.image_data` (base64 for LLM vision)

## File Locations
- App YAML: `examples/opencode/app.yaml`
- Tool names: `packages/digitorn/core/runtime/tool_names.py`
- Tool exec dispatch: `packages/digitorn/core/runtime/tool_exec.py`
- Agent loop: `packages/digitorn/core/runtime/agent_loop.py`
- Streaming: `packages/digitorn/core/runtime/streaming.py`
- Image store: `packages/digitorn/core/image_store.py`
- Multimodal: `packages/digitorn/core/runtime/multimodal.py`
- Hooks V2: `packages/digitorn/core/runtime/hooks.py`
- Config system: `packages/digitorn/core/config.py`
- Middleware system: `packages/digitorn/core/middleware.py`
- Middleware store: `packages/digitorn/core/middleware_store.py`
- API routes (apps): `packages/digitorn/core/api/apps.py`
- Error classification: `packages/digitorn/core/api/apps.py` (`_classify_error`)
- Compiler: `packages/digitorn/core/app/compiler.py`
- Background mode: `packages/digitorn/core/runtime/modes/background.py`
- Anthropic provider: `packages/digitorn/modules/llm_provider/providers/anthropic.py`
- Filesystem module: `packages/digitorn/modules/filesystem/module.py`
- Filesystem params: `packages/digitorn/modules/filesystem/params.py`
- Workspace module: `packages/digitorn/modules/workspace/module.py`
- Preview module: `packages/digitorn/modules/preview/module.py`
- LSP module: `packages/digitorn/modules/lsp/module.py`
- LSP parsers (built-in validators): `packages/digitorn/modules/lsp/parsers.py`
- Builder app: `packages/digitorn/builtins/digitorn-builder/app.yaml`
- Schema (WorkspaceBlock): `packages/digitorn/core/app/schema.py`
- Shell module: `packages/digitorn/modules/shell/module.py`
- Shell adapters: `packages/digitorn/modules/shell/platform_adapters.py`
- Memory module: `packages/digitorn/modules/memory/module.py`
- Agent spawn module: `packages/digitorn/modules/agent_spawn/module.py`
- Agent spawn runner: `packages/digitorn/modules/agent_spawn/runner.py`
- Agent spawn params: `packages/digitorn/modules/agent_spawn/params.py`
- Loop guards: `packages/digitorn/core/runtime/loop_guards.py`
- Tool display: `packages/digitorn/core/runtime/tool_display.py`
- Behavior module: `packages/digitorn/modules/behavior/module.py`
- Behavior engine: `packages/digitorn/modules/behavior/engine.py`
- Behavior rules: `packages/digitorn/modules/behavior/rules.py`
- Behavior classifier: `packages/digitorn/modules/behavior/classifier.py`
- Behavior profiles: `packages/digitorn/modules/behavior/profiles.py`
- Behavior state: `packages/digitorn/modules/behavior/state.py`
- Dev CLI: `packages/digitorn/core/cli/dev.py`
- Dev CLI test app: `packages/digitorn/builtins/digitorn-code/app-test.yaml`
- Digitorn Code app: `packages/digitorn/builtins/digitorn-code/app.yaml`
- Digitorn DeepResearch app: `packages/digitorn/builtins/digitorn-deepresearch/app.yaml`
- Context builder: `packages/digitorn/modules/context_builder/`
- Base module (auto-coerce): `packages/digitorn/modules/base.py`
- Manifest (ActionSpec): `packages/digitorn/modules/manifest.py`
- Decorators (@action): `packages/digitorn/modules/decorators.py`
- TUI app: `packages/digitorn/core/cli/tui/app.py`
- Chat log: `packages/digitorn/core/cli/tui/widgets/chat_log.py`
- Sidebar: `packages/digitorn/core/cli/tui/widgets/sidebar.py`
- Status footer: `packages/digitorn/core/cli/tui/widgets/status_footer.py`
- Standalone backend: `packages/digitorn/core/cli/tui/backends/standalone.py`
- Daemon backend: `packages/digitorn/core/cli/tui/backends/daemon.py`
- Stability test: `test_stability.py` (run with `py -3.12 test_stability.py`)

## Important Rules
- NEVER use `git stash`, `git reset`, or any destructive git command - team project
- ALWAYS test changes before telling the user to restart
- ALWAYS sync to site-packages if editable mode isn't working: check with `py -3.12 -c "import digitorn; print(digitorn.__file__)"`
- The daemon caches code - MUST restart after changes
- Three copies may exist: packages/, build/lib/, site-packages/ - editable mode should use packages/ directly
- Python 3.12 required (pyproject.toml: python = "^3.12")

## Live testing - where to put what

- `packages/digitorn/testing/` is the **SDK** (DevClient, LiveEventStream, assertions). It is a library, NOT a collection of tests. Read `packages/digitorn/testing/README.md` before touching anything there.
- Test scenarios live OUTSIDE the SDK, typically in `tools/live_tests/<feature>_scenarios.py`. They import `digitorn.testing` as any consumer would.
- When writing a new live test, the default is: add it under `tools/live_tests/`. Only extend the SDK when you need a genuinely reusable primitive.

## Two storage locations for builtin apps - DO NOT CONFUSE

When a builtin app (digitorn-builder, digitorn-react-sandbox, ...) is deployed,
its files exist in **two distinct places**:

1. `~/.digitorn/packages/<app_id>/` - the **package install dir**, owned by
   `installed_packages` registry. Holds the canonical source tree (incl.
   `web/dist/` if pre-built). Path: `installed_packages.install_dir`.
2. `~/.digitorn/apps/<app_id>/bundle-<hash>/` - the **bundle dir**, owned by
   `app_bundles` table. Holds the compiled snapshot the daemon actually
   reads at `reload_from_db`. Only `app.yaml` + `meta.json` (no web/).
   Path: `app_bundles.bundle_path`, current one in `applications.current_bundle_id`.

When patching a builtin's app.yaml, you usually need to patch BOTH the source
under `packages/digitorn/builtins/<app>/app.yaml` AND the deployed bundle
under `~/.digitorn/apps/<app>/bundle-<hash>/app.yaml` (lookup current_bundle_id
in `digitorn.db`). The bootstrap upgrade flow should sync them but may not
in dev (hash mismatch / failed upgrade leaves bundle stale).

## Module config YAML structure - `config:` wrapper REQUIRED

`ModuleBlock` (Pydantic schema in `core/app/schema.py`) has exactly 4 known
fields: `config`, `setup`, `constraints`, `middleware`. Anything else under a
module block is **silently dropped**. So this is wrong:

```yaml
modules:
  rag:
    backend:           # ← IGNORED, never reaches the module
      type: qdrant
      path: "..."
```

Correct form:

```yaml
modules:
  rag:
    config:
      backend:
        type: qdrant
        path: "..."
```

Without the wrapper, `compiled.modules["rag"].config = {}`, the bootstrap
sees `if config:` as False, and **never calls `module.on_config_update`**.
For the rag module that means the qdrant backend stays at its in-memory
default - every query returns "knowledge base not found".

## RAG module - shared instance, per-app reconfig

The rag module is `isolation = "shared"` (one instance for the whole daemon).
Its `on_start()` runs ONCE at daemon start with whatever empty config the
module has at that moment → default in-memory backend.

When an app is activated, the bootstrap calls `module.on_config_update(cfg)`
with that app's config. The base `BaseModule.on_config_update` only stores
the dict - it does NOT re-create the backend. So the rag module overrides
`on_config_update` (in `modules/rag/module.py`) to:

1. Compare old vs new backend path
2. Close the old backend if changed
3. Re-create + initialize the new backend with the new path
4. Call `_discover_existing_collections()` to rebuild `_kbs` from the
   collections already on disk (populated by `knowledge_base/build.py` or
   any offline tool)

This is the only way a `shared` module can hold per-app state. Other shared
modules (cron_native, cache, vector) get an `_app_id_override` injected by
`_inject_app_id_overrides` in bootstrap.py - rag does NOT (it intentionally
shares storage so multiple apps can see the same KBs).

## Loopback auth bypass (agent self-calls)

The agent runs IN the daemon process. When it makes `http.get("http://127.0.0.1:8000/...")`,
the request goes out a socket and back in via the daemon's HTTP server. The
auth middleware sees a fresh request with no Authorization header → 401, and
the agent has no JWT to give itself.

`auth/middleware.py::_is_loopback_self_call(request)` resolves this:
- Trigger: `request.client.host` ∈ `{127.0.0.1, ::1, localhost}` (real TCP IP,
  not a header - cannot be spoofed) AND
- Path starts with one of `_LOOPBACK_AGENT_PATH_PREFIXES`:
  `/api/discovery/`, `/api/apps/`, `/api/credentials/providers`, `/api/health`

When both match, auth is bypassed and `request.state.user_id = "system"` with
permissions `["*"]`. ALL other paths (sessions write, messages, credentials
write, auth/*) require JWT even from loopback. Add new paths to that allow-list
ONLY if they're read-mostly and safe to expose to in-process agents.

## Cross-platform process group (no orphans on terminal close)

`core/process_group.py::install()` is called early in `server.py::start()`.
On Windows it creates a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`
and assigns the daemon to it - when the daemon exits for ANY reason (Ctrl+C,
kill, terminal close, crash), Windows automatically terminates every child in
the job. On Linux it calls `setpgrp()` and applies `PR_SET_PDEATHSIG=SIGKILL`
on each child via `set_pdeathsig_on_child(popen_kwargs)`. On macOS it relies
on `setpgrp` + atexit `killpg`.

`subprocess.Popen` calls that need to be in the kill-on-parent-death group
should use `set_pdeathsig_on_child(kwargs)` to inject the `preexec_fn` on
Linux. Windows children inherit the job automatically.

## Bootstrap_builtins - background fire-and-forget

`bootstrap_builtins` (called by `server.py` lifespan) is dispatched via
`asyncio.create_task` so the lifespan returns immediately and Uvicorn starts
serving. Per-package timeout: 60s (`_PER_PKG_TIMEOUT`). Global timeout: 600s.
A failing or hanging upgrade marks the package `BROKEN` and the daemon keeps
running with the previously-deployed version. **Never await** bootstrap in
the lifespan path - it would block the HTTP server on dev-time recompiles.

## Patch-in-place upgrade (no rename swap)

`InstallFlow.upgrade` uses `_patch_in_place(src, dst)` (in `install.py`)
instead of `dir.rename` because Windows refuses to rename a directory if any
process holds an open handle inside it (Vite watching node_modules,
antivirus, file indexer). The patch walks `src` with `os.walk`, skips
`_PRESERVE_DIRS` (`node_modules`, `dist`, `.vite`, `.next`, `.turbo`,
`.cache`, `__pycache__`, `build`, `.output`, `.svelte-kit`, `.digitorn`),
and `shutil.copy2`'s each file over the matching path in `dst`. Runs in
`asyncio.to_thread` with a 20s timeout. Never renames the install dir
itself, so the install dir's handle is never required.

## Preview module - internal SSE transport layer

The preview module (`modules/preview/`) is the SSE transport layer. ALL 17
actions are `internal=True` - invisible to LLM agents. The workspace module
calls them as Python methods (`self._preview.set_resource(...)`).

Three primitive ops, ALL per-session:
- `set_state(key, value)` / `patch_state(patch)` - key-value scalar map
- `set_resource(channel, id, payload)` / `patch_resource` / `delete_resource` /
  `bulk_set_resources(channel, items, replace=False)` / `clear_channel` -
  named maps of arbitrary payloads (e.g. `nodes`, `edges`, `files`, `slides`)
- `emit(event_type, data)` - fire-and-forget event

## Workspace module - the agent's file API for live apps

`modules/workspace/module.py` - **6 actions** exposed to agents:
`write`, `read`, `edit`, `glob`, `grep`, `delete`. Tool names: WsWrite,
WsRead, WsEdit, WsGlob, WsGrep, WsDelete.

Under the hood every mutation calls `preview.set_resource("files", ...)`,
streaming changes to the client in real time. The agent uses the same API
pattern as filesystem - it doesn't know files live in memory.

### Workspace config (app.yaml)

```yaml
modules:
  workspace:
    config:
      render_mode: react      # react | builder | latex | slides | html | markdown | code | auto
      entry_file: src/App.tsx  # main file for the client to render first
      title: My App
      sync_to_disk: false      # mirror writes to real filesystem (Lovable-style)
      sync_path: null          # disk dir (defaults to app workspace dir)
      lint: true               # run diagnostics on every write/edit
      auto_approve: false      # bypass validation - every write lands approved
      instructions: |          # prepended to all workspace tool prompts
        You are building a React app...
      tool_instructions:       # per-tool override (keys: write, read, edit, glob, grep, delete)
        write: "Custom write instructions..."
```

### Workspace params - minimal visible, powerful hidden

| Action | Visible params | Hidden params |
|--------|---------------|---------------|
| write  | path, content | - |
| read   | path | offset, limit |
| edit   | path, old_string, new_string | replace_all, insert_at_line, fuzzy_threshold, max_suggestions |
| glob   | pattern | sort_by |
| grep   | pattern | glob, case_insensitive, multiline, before, after, max_results |
| delete | path | - |

### sync_to_disk - workspace <-> real filesystem

When `sync_to_disk: true`, every workspace mutation is mirrored to disk:
- `write` / `edit` -> writes updated content to `{sync_dir}/{path}`
- `delete` -> removes file from disk
- `read` -> **read-through**: if file not in memory but exists on disk, loads it
- `glob` / `grep` -> scans disk for files not yet loaded, then searches all

This replaces the need for a separate `filesystem` module in apps that
generate real code (Lovable-style, React sandboxes, LaTeX, etc.).

### lint - built-in diagnostics on write/edit

When `lint: true` (default), every `write` and `edit` returns diagnostics
inline in the tool response. Resolution order:
1. **LSP module** (if loaded): `lsp.notify_change(path, content)` -> real
   language server (texlab, pyright, ruff, eslint, etc.)
2. **Built-in content validators**: JSON, YAML, TOML, Python syntax, LaTeX
   (unmatched braces + environments) - work in-memory, no external tools

The agent never needs to call `lsp.diagnostics()` separately.

### Validation workflow (approve / reject / diff / history)

Every `WsWrite` / `WsEdit` emits a `resource_patched` event on the
`files` channel with `validation: "pending"` (unless `auto_approve` is
on). The payload carries:
- `insertions_pending`, `deletions_pending` - **delta vs the last-approved baseline**, NOT cumulative. After `approve()` they reset to 0; after a 1-line edit they show 1/1.
- `total_insertions`, `total_deletions` - cumulative session totals.
- `baseline_lines` - line count of the last-approved snapshot.
- `source: "user"` when written via PUT writeback (absent for agent writes).
- `git_status` when the workspace is a git repo.

Endpoints (all under `/api/apps/{app_id}/sessions/{sid}/workspace/`):

| Method | Path | Purpose |
|---|---|---|
| GET  | `files/{path}?include_baseline=true` | Content + baseline + `unified_diff_pending` (well-formed, parseable by `difflib.PatchSet`) |
| GET  | `files/{path}/history` | Revision list (`revision, approved_at, approved_by, tokens_delta_ins/del`) |
| POST | `files/approve` | Stage whole file - baseline = current content |
| POST | `files/reject` | Revert to baseline, or delete if never approved |
| POST | `files/approve-hunks` | Partial stage by hunk index or 12-char hash |
| POST | `files/reject-hunks` | Partial revert by hunk index or hash |
| PUT  | `files/{path}` | User writeback (manual edit, conflict resolution, drag-drop import) |
| POST | `commit` | `git add` + `git commit` over approved files |
| POST | `git-status` | Refresh git_status flags on every tracked file |

Hunks have stable 12-char SHA-256 identifiers (header+body) - the
client can approve by hash instead of index to survive races with
concurrent agent writes. The `approve-hunks` implementation applies
hunks in reverse position order so earlier indices aren't perturbed
by later length changes.

Baseline + history persist to
`{ws}/.digitorn/sessions/{sid}/baselines/{path}` (baseline) and
`{ws}/.digitorn/sessions/{sid}/baselines/{path}.history/rev-NNNN` +
`_index.json`. Survives daemon restart.

### `auto_approve` mode - bypass validation entirely

```yaml
modules:
  workspace:
    config:
      auto_approve: true
```

Every write/edit lands with `validation: "approved"`, pending counters
always zero, baseline = current content on each mutation. For sandbox
apps / trusted-agent pipelines / CI. Per-call override via
`PUT /workspace/files/{path} {auto_approve: true}` when you want the
bypass for a single writeback without flipping the module-level flag.

### Bootstrap wiring

In `bootstrap.py`:
- `workspace._preview = preview_module` - SSE transport
- `workspace._lsp = lsp_module` - diagnostics (if LSP module loaded)
- Top-level `workspace:` block -> injects `render_mode`, `entry_file`, `title`

### Top-level workspace: block (for Flutter client)

```yaml
workspace:
  render_mode: builder   # Flutter reads this from API summary
  entry_file: app.yaml
  title: "My App"
```

Added in `schema.py` as `WorkspaceBlock`, compiled in `compiler.py`,
exposed via `manager.py:summary()`. Flutter uses this to pick render mode.

## Static-bundle preview vs Vite dev server

Two preview modes coexist:

1. **`mode: dev_server`** - `preview.enabled: true`, daemon spawns Vite,
   proxies HTTP + WebSocket. Heavy (~150 MB RAM/app) but supports HMR.

2. **`mode: static`** - `preview.enabled: false` AND `web/dist/index.html`
   exists. Daemon serves static files directly. Zero process per app.

## Building new live app types

Recipe:
1. Create `packages/digitorn/builtins/<app-id>/app.yaml` with
   `modules: { preview: {}, workspace: { config: { render_mode: ... } } }`
2. Create `web/` with React + Vite using `usePreviewResources("files")`
3. `npm install && npx vite build` once
4. Restart daemon
