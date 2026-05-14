---
id: context_builder
title: context_builder Module
sidebar_label: context_builder
sidebar_position: 3
description: Orchestration engine - tool indexing, system-prompt assembly, execution routing, primitives (parallel / background / watchers / ask_user / call_app / use_skill).
---

# context_builder

The context builder is the **central nervous system** of
every Digitorn application. It sits between the agent and
everything else: modules, providers, memory, security, and
the outside world. Every tool call passes through it. Every
system prompt is assembled by it. Every background task and
watcher is managed by it.

| Property | Value | Source |
|----------|-------|--------|
| Module id | `context_builder` | `module.py` |
| Version | `1.0.0` | `module.py` |
| Type | system (auto-loaded for every application) | |
| Action count | 17 | `actions_meta.py` (9) + `actions_background.py` (1, with 5 modes) + `actions_watchers.py` (7) |

## What it does

Five core functions:

### 1. Tool indexing

At app start, scans every loaded module and builds a
searchable index of all available actions. Per action it
records:

- Fully qualified name (`filesystem.read`, `git.status`).
- The `@action` description.
- Tags + multilingual aliases.
- Parameter names + descriptions.
- Side effects + risk level.
- Synonym expansions (e.g. *delete* indexes *remove*,
  *destroy*, *erase*).

The index powers two strategies:

- **Keyword search** - exact / prefix / fuzzy on action name,
  module, aliases.
- **Semantic search** - descriptions, tags, params embedded
  with FastEmbed
  (`paraphrase-multilingual-MiniLM-L12-v2`, 384 dims, ~50
  languages) and stored in an in-memory Qdrant HNSW index.

Final ranking: `semantic_score * 10 + keyword_boost` -
semantic dominates, but exact-name matches get a significant
bonus.

### 2. System prompt assembly

`prompt.py`. The system prompt is assembled dynamically from
multiple sources in this order:

1. Memory snapshot (goal, tasks, facts).
2. Memory instructions (how to use memory tools).
3. Agent identity (`You are agent X, role Y`).
4. Tool instructions (discovery vs. direct mode).
5. Structural hints (parameter templates, JSON examples).
6. Agent pool info (available specialists, when role is
   coordinator).
7. Skills list (available `/commands`).
8. Channel info (available notification targets).
9. **User `system_prompt:`** (always last - personality +
   behaviour).

The user only defines personality + behaviour; the context
builder provides everything else. The prompt adapts based on
tool injection mode (discovery vs direct), native vs
text-based tool calling, active modules, memory state, and
agent role.

### 3. Execution routing

When the agent calls a tool, the context builder:

1. Resolves module + action from the FQN (with fuzzy
   "did you mean?" suggestions).
2. Runs the security gate (`grant` / `deny` / `approve`
   from `tools.capabilities`).
3. Validates params against the Pydantic model + auto-coerces
   types where safe.
4. Routes MCP virtual tools to the right server.
5. Embeds schema hints in error messages on bad params.

### 4. Execution primitives

Always available to every agent regardless of YAML config:

- **Parallel execution** - `run_parallel` runs many actions
  in one turn.
- **Background tasks** - `background_run` (one action, 5
  modes) launches non-blocking work.
- **Watchers** - `watch_start` + 6 lifecycle actions monitor
  predicates over time.
- **AskUser** - `ask_user` pauses execution + blocks for
  human approval.
- **Call other apps** - `call_app` invokes a deployed app as
  a sub-tool.
- **Skills** - `use_skill` loads reusable workflow markdown
  on demand.

### 5. Adaptive tool injection

Decides how to present tools to the agent:

| Factor | Threshold | Result |
|--------|-----------|--------|
| `total_tools * 200` tokens vs 20 % of context window | tools fit | **direct mode** - all tools as native function schemas. |
| Tools exceed budget | | **discovery mode** - only 5 meta-tools, agent uses `search_tools` + `execute_tool`. |

Operational tools (memory, agent_spawn) are **always**
injected directly - the agent should never need to discover
how to manage its own memory.

## The 18 LLM-exposed actions

### Tool discovery (5) - `actions_meta.py`

| Action | Purpose |
|--------|---------|
| `context_builder.search_tools` | Hybrid semantic + keyword search across all indexed actions. |
| `context_builder.get_tool` | Full schema, metadata, examples for one action. |
| `context_builder.execute_tool` | Execute any action by name with params. |
| `context_builder.list_categories` | List all loaded modules + descriptions. |
| `context_builder.browse_category` | List actions in a specific module. |

### Parallel + background (2)

| Action | Purpose |
|--------|---------|
| `context_builder.run_parallel` | Run many tools concurrently in one turn. |
| `context_builder.background_run` | One action, 5 modes (launch / status / wait / cancel / list). |

`background_run` modes (dispatched by params):

- `tool=...` → launch (returns `task_id`).
- `task_id=...` → status.
- `task_id=..., wait=true` → block until done.
- `task_id=..., cancel=true` → cancel.
- `list=true` → list all background tasks for the session.

### Watchers (7) - `actions_watchers.py`

Persistent monitors that poll a predicate + interval and
fire follow-up actions when it changes.

| Action | Purpose |
|--------|---------|
| `context_builder.watch_start` | Start a watcher (predicate + interval + actions). |
| `context_builder.watch_stop` | Stop and remove a watcher. |
| `context_builder.watch_pause` | Pause a running watcher. |
| `context_builder.watch_resume` | Resume a paused watcher. |
| `context_builder.watch_status` | Detailed status + metrics. |
| `context_builder.watch_list` | List all watchers. |
| `context_builder.watch_history` | Last N check results. |

Requires `runtime.watchers: true` in the app YAML.

### Other (4)

| Action | Purpose |
|--------|---------|
| `context_builder.use_skill` | Load a reusable workflow on demand. |
| `context_builder.call_app` | Call another deployed app as a sub-tool. |
| `context_builder.ask_user` | Pause execution + ask the user a question (with optional reviewable / editable content). |
| `context_builder.cancel_bg_task` | Internal - cancel a session's bg task on cleanup. |

### Removed / moved

- **Workbench actions** (`wb_*`) - removed in the workbench
  → workspace migration. Use the
  [workspace](workspace.md) module
  (`WsWrite`, `WsRead`, `WsEdit`, ...).
- **Scheduler actions** (`schedule_*`) - moved to the
  [cron_native](cron_native.md) module.
- **`send_notification`** - removed. Use the
  [channels](channels.md) module's `reply` /
  `send_message` instead.

## `ask_user` - agent-initiated approval workflow

`actions_meta.py`. Pauses the agent loop via the
`ApprovalQueue` (`asyncio.Future`) until the user answers.
When `content` is provided, the user can view + edit it
before approving.

| Param | Type | Required | Default | Description |
|-------|------|:--------:|:-------:|-------------|
| `question` | string | yes | - | Question to show. |
| `content` | string | no | `null` | Reviewable / editable content (markdown). Displayed in the workspace. |
| `timeout` | float | no | `300` | Max seconds to wait. |

Returns:

```json
{
  "status": "approved",
  "question": "Review this plan?",
  "content": "## Plan\n1. Create middleware\n...",
  "content_was_edited": false
}
```

Or on rejection:

```json
{
  "status": "denied",
  "question": "Review this plan?",
  "user_feedback": "Use JWT instead of sessions"
}
```

UI:

- TUI - question shown with markdown rendering, sidebar
  shows plan steps.
- Web client - `ApprovalBanner` renders with full markdown,
  workspace opens `plan.md` in split mode.

To expose, add to `tools.capabilities.grant`:

```yaml
tools:
  capabilities:
    grant:
      - {module: context_builder, actions: [ask_user]}
```

## Internal components:

| File | Responsibility |
|------|----------------|
| `module.py` | Action dispatch + background task management + watcher lifecycle + notification delivery. |
| `prompt.py` | System prompt assembly + tool instruction generation + structural hints + MCP workflow hints. |
| `builder.py` | Tool index construction + direct tool schema generation + MCP risk inference. |
| `scoring.py` | Hybrid search engine + synonym expansion + tokenization. |
| `embeddings.py` | FastEmbed model loading + semantic index (Qdrant) + embedding + query. |
| `actions_meta.py` | Tool discovery actions, run_parallel, ask_user, call_app, use_skill. |
| `actions_background.py` | `background_run` (one action, 5 modes). |
| `actions_watchers.py` | 7 watcher actions. |
| `tool_schema.py` | JSON schema generation, hidden-param filtering. |

## Configuration

The context builder needs no explicit config. It's
configured implicitly by:

- Modules in YAML → indexed tools.
- Agent brain settings → tool injection mode.
- `tools.capabilities` → action permissions.
- `tools.channels` → notification targets.
- `agents[].capabilities` (skills) → loadable workflows.
- `memory` module presence → memory snapshot injection.
- `agent_spawn` module presence → agent pool info.

## Cross-references

- App-config block reference (no direct config - implicit):
  [App Configuration](../../language/02-app-config.md)
- Tool injection modes (discovery vs direct):
  [Tool Injection](../../language/04-tools.md)
- Skills system (`use_skill`):
  [Skills System](../../language/21-skills.md)
- AskUser semantics + approval flow:
  [Security → Resolving a policy](../../language/11-security.md#resolving-a-policy)
- Watchers + scheduler:
  [cron_native reference](cron_native.md)
