---
id: 04b-builtin-tools
---

# Built-in Tools

Some tools are exposed to the agent without declaring a `tools.modules`
entry — they're either always-available primitives provided by the
`context_builder` module, or short-named aliases the runtime adds when
the corresponding module is loaded.

This page is the authoritative list. Every entry maps to a real
`@action` in the codebase; entries are cited with file + line.

## Always-available primitives (`context_builder`)

`context_builder` is auto-loaded by the runtime (no YAML declaration
needed). It exposes 9 actions in
`packages/digitorn/modules/context_builder/actions_meta.py` plus
1 action in `actions_background.py`. Every agent in every mode
(direct / compact / discovery) gets access to these.

| Tool | Source | Description |
|------|--------|-------------|
| `search_tools` | `actions_meta.py` | Hybrid (semantic + keyword) search over the agent's visible tool index. |
| `get_tool` | `actions_meta.py` | Full JSON schema + examples + side-effects + aliases for one tool. |
| `execute_tool` | `actions_meta.py` | Execute a tool by name with parameters. The agent loop also auto-routes direct calls (e.g. `filesystem.read({...})`) through this action. |
| `list_categories` | `actions_meta.py` | List all tool domains (modules) currently visible to this agent. |
| `browse_category` | `actions_meta.py` | Paginated list of tools in one domain. |
| `run_parallel` | `actions_meta.py` | Execute multiple tool calls concurrently via `asyncio.gather`. |
| `use_skill` | `actions_meta.py` | Invoke a `/command` skill from `dev.skills` or the bundle's `skills/` dir. |
| `call_app` | `actions_meta.py` | Call another deployed Digitorn app as a tool (composition pattern). |
| `ask_user` | `actions_meta.py` | Pause the loop and ask the user a typed question (HITL). |
| `background_run` | `actions_background.py` | Launch any tool as a background task. **One action, five modes** dispatched by params: launch (`name`+`params`), status (`task_id`), cancel (`task_id`+`cancel=true`), wait (`task_id`+`wait=true`+`timeout`), list (`list_tasks=true`). |

Short-name aliases registered in
`packages/digitorn/core/runtime/tool_names.py`:

| Short alias | FQN |
|-------------|-----|
| `AskUser` | `context_builder.ask_user` |
| `BackgroundRun` | `context_builder.background_run` |
| `Agent` | `agent_spawn.agent` (when the `agent_spawn` module is loaded — see below) |

## Watcher primitives (gated by `runtime.watchers: true`)

`packages/digitorn/modules/context_builder/actions_watchers.py`. Seven
actions appear in the agent's tool index when `runtime.watchers` is
enabled.

| Tool | Source | Description |
|------|--------|-------------|
| `watch_start` | `actions_watchers.py` | Start a periodic watcher that runs a tool every N seconds and records the result. |
| `watch_stop` | `actions_watchers.py` | Stop a watcher (frees its slot). |
| `watch_pause` | `actions_watchers.py` | Pause a running watcher. |
| `watch_resume` | `actions_watchers.py` | Resume a paused watcher. |
| `watch_status` | `actions_watchers.py` | Get a single watcher's status, last result, run count, error rate. |
| `watch_list` | `actions_watchers.py` | List all watchers (active + paused). |
| `watch_history` | `actions_watchers.py` | Last N check results for one watcher. |

Watchers are persistent — they survive across turns. Disable them
explicitly when the app no longer needs them (set `runtime.watchers:
false` and redeploy, or call `watch_stop` from a turn).

## Scheduler primitives (gated by `runtime.scheduler: true`)

Provided by the `cron_native` module
(`packages/digitorn/modules/cron_native/module.py`). Three actions:

| Tool | Source | Description |
|------|--------|-------------|
| `schedule` | `cron_native/module.py` | Schedule a tool call. Accepts `at: <ISO timestamp>` (one-shot), `delay: <seconds>` (one-shot), or `cron: <expr>` (recurring). |
| `cancel_schedule` | `cron_native/module.py` | Cancel a scheduled job by id. |
| `remind` | `cron_native/module.py` | Convenience wrapper that schedules a "self-prompt" reminder back to the agent at a given time. |

`runtime.scheduler: true` requires `runtime.watchers: true`
(`schema.py` `RuntimeBlock.scheduler`).

> The legacy `schedule_once`, `schedule_cron`, `schedule_cancel`,
> `schedule_list`, `schedule_status` action names referenced in older
> docs **do not exist** in the code. Use the three actions above.

## Memory tools (gated by `tools.modules.memory`)

When `memory` is declared under `tools.modules`, four actions become
available
(`packages/digitorn/modules/memory/module.py` —
all four `@action` decorators).

| Tool | Short alias | Source | Description |
|------|-------------|--------|-------------|
| `memory.task_create` | `TaskCreate` | `memory/module.py` | Create a task in the agent's working-memory todo list. |
| `memory.task_update` | `TaskUpdate` | `memory/module.py` | Update a task's status (`pending`, `in_progress`, `done`, `blocked`) or other fields. |
| `memory.set_goal` | — | `memory/module.py` | Set or update the agent's current high-level goal. |
| `memory.remember` | `Remember` | `memory/module.py` | Store a long-term fact that survives compaction. Searchable via the same module's recall layer at runtime. |

Short aliases come from `tool_names.py`. Anything else (set_plan,
update_plan_step, add_todo, add_fact, recall, forget, track_entity,
add_relationship, checkpoint, cache_content, get_snapshot,
add_episode, ...) **does not exist as an `@action`** — those names
appeared in older drafts and were never implemented. The four actions
above are the entire surface of the `memory` module today.

See [Cognitive Memory](05-memory.md) for how the agent uses these in
practice.

## Agent-spawn tool (gated by `agent_spawn` module loaded)

`packages/digitorn/modules/agent_spawn/module.py` exposes a
**single `@action`** named `agent`. Eight modes are dispatched by
params (the implementation routes through the `_mode_*` private
methods at line 373-765).

```yaml
# Compile-time config: load the module via capabilities or per-agent grant
tools:
  capabilities:
    grant:
      - { module: agent_spawn }
```

| Mode | Params | Result |
|------|--------|--------|
| Spawn (sync) | `prompt` (+ optional `specialist`, `wait=true` default) | Returns the spawned agent's final result. Blocks the parent turn. |
| Spawn (async) | `prompt`, `wait=false` | Returns `agent_id` immediately. Background. |
| Status | `agent_id` | Current state (`running` / `done` / `failed` / `cancelled`). |
| Wait one | `agent_id`, `wait=true`, `timeout` | Block until that agent finishes. |
| Wait many | `agent_ids: [a, b, ...]` (or omit for all) | Block until each is done; returns each result. |
| Cancel | `agent_id`, `cancel=true` | Force-cancel a running spawn. |
| Reassign | `agent_id`, `reassign: <new prompt>` | Cancel + respawn the same id with a new task. |
| List | `list=true` | All current spawns and their status. |

Short alias: `Agent` → `agent_spawn.agent` (`tool_names.py`).

> The legacy short names `AgentWait`, `AgentWaitAll`, `AgentResult`,
> `AgentStatus`, `AgentCancel`, `AgentList`, `ReassignAgent`
> referenced in older docs **do not exist** as separate tools. Every
> mode is reached via the single `Agent`/`agent_spawn.agent` action
> with the appropriate params.

See [Multi-Agent](12-multi-agent.md) for orchestration patterns.

## Workspace tools (gated by `tools.modules.workspace`)

When the `workspace` module is loaded, six short-named actions
(`tool_names.py`) appear in the agent's index. They operate on
the in-memory virtual filesystem streamed to the client via
Socket.IO, not the OS filesystem.

| Short | FQN |
|-------|-----|
| `WsWrite` | `workspace.write` |
| `WsRead` | `workspace.read` |
| `WsEdit` | `workspace.edit` |
| `WsGlob` | `workspace.glob` |
| `WsGrep` | `workspace.grep` |
| `WsDelete` | `workspace.delete` |

See [Workspace & Preview](41-preview.md) for the renderer modes
(react / vue / latex / slides / code) and the live-preview pipeline.

## Web-preview tools (gated by `tools.modules.web_preview`)

When the `web_preview` module is loaded, four short-named actions
appear in the agent's index. They drive the iframe preview at
`/api/apps/<id>/preview/?session_id=<sid>` — the agent attaches it
to a dev server it spawned (proxy) or to a directory it built
(static), per session.

| Short | FQN | Purpose |
|-------|-----|---------|
| `PreviewProxy` | `web_preview.proxy` | Proxy the iframe to a TCP port the agent's dev server is listening on. |
| `PreviewStatic` | `web_preview.static` | Serve a directory under the session workspace as static files. |
| `PreviewDetach` | `web_preview.detach` | Drop a previously-registered attachment by name. |
| `PreviewList` | `web_preview.list` | List the active attachments for the current session. |

The agent is responsible for spawning dev servers itself
(`Bash(run_in_background=true)`) and resolving port conflicts. The
daemon never spawns servers on its own. See
[Workspace & Preview → web_preview](41-preview.md#toolsmodulesweb_preview--session-scoped-iframe-attachments)
for the three regimes (proxy / static / declarative) and the
LLM communication contract.

## Other short-name aliases

Defined in `tool_names.py` and active when the corresponding
module is loaded:

| Short | FQN | Module |
|-------|-----|--------|
| `Read`, `Write`, `Edit`, `Grep`, `Glob`, `Ls` | `filesystem.<name>` | `filesystem` |
| `Bash` | `shell.bash` | `shell` (5 modes via params) |
| `WebSearch`, `WebFetch` | `web.search`, `web.fetch` | `web` |
| `LintCheck`, `LintFile` | `lsp.diagnostics`, `lsp.check` | `lsp` |
| `DbConnect`, `DbDisconnect`, `DbList`, `DbQuery`, `DbTransaction`, `DbBulkInsert`, `DbSchema`, `DbBrowse`, `DbRelations`, `DbSearch` | `database.<name>` | `database` (10 actions) |

The full mapping (single source of truth) is in
`packages/digitorn/core/runtime/tool_names.py`.

## Tool injection — what the agent actually sees

Whether a built-in shows up in the agent's tool index depends on the
[tool injection mode](04-tools.md#adaptive-tool-injection):

| Injection | Meta + always-available primitives | Module tools (gated short aliases above) | Domain tools (filesystem, database, http, ...) |
|-----------|-----------------------------------|------------------------------------------|------------------------------------------------|
| `direct` | Direct (full schemas) | Direct | Direct |
| `compact_direct` | Direct (full schemas) | Direct (compact: name + 1-line) | Direct (compact) |
| `discovery` | Direct (always) | Direct (the gated short aliases stay direct) | Behind `search_tools` / `browse_category` / `get_tool` / `execute_tool` |

In every mode the meta-tools and the watcher / scheduler / agent_spawn
/ memory / workspace gated tools are **always directly callable** —
the agent never has to "discover" them. Discovery mode only hides
non-strategic domain tools.

## Cross-references

- Tool delivery algorithm: [Tools](04-tools.md)
- Per-module reference (full action list per module):
  [modules/index.md](../reference/modules/)
- Working memory model: [Cognitive Memory](05-memory.md)
- Spawning specialists: [Multi-Agent](12-multi-agent.md)
- Live workspace + preview: [Workspace & Preview](41-preview.md)
- Capabilities (grant / approve / deny):
  [App Configuration → tools.capabilities](02-app-config.md#toolscapabilities--grant--approve--deny),
  [Security](11-security.md)
