---
id: hooks
title: Hooks V2
sidebar_label: Hooks
sidebar_position: 6
description: Condition-action hooks fired during the agent loop - 15 events, 14 conditions, 13 general actions + 5 builder-specific.
---

# Hooks V2

Hooks are condition-action pairs that fire at specific
points in the agent loop. They let you control behaviour
declaratively in YAML, no Python required.

## Quick example

```yaml
runtime:
  hooks:
    - id: auto-compact
      on: turn_end
      condition:
        type: context_pressure
        threshold: 0.80
      action:
        type: compact_context
        strategy: summarize
        keep_recent: 12
      cooldown: 60
```

A hook has:

- **`on`** - the event that triggers evaluation.
- **`condition`** - when to fire (sync-evaluated).
- **`action`** - what to do.
- **`cooldown`** - minimum seconds between firings (optional).

> **Canonical block**: hooks live under `runtime.hooks` (or
> `agents[].hooks` for per-agent specialists). Legacy
> `execution.hooks` is auto-aliased.

## Hook schema

`HookConfig`.

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `id` | string | required | Unique within scope. |
| `on` | string | `"turn_end"` | Event name (15 valid). Aliases `pre_tool_use` / `post_tool_use` / `user_prompt` resolve to `tool_start` / `tool_end` / `turn_start`. |
| `condition` | object | `{type: always}` | One of 14 types. |
| `action` | object | required | One of 13 general types (or 5 builder-specific). |
| `cooldown` | float (s) | `0` | Min seconds between fires. |
| `max_fires` | int | `0` | Lifetime cap. `0` = unlimited. |
| `priority` | int | `100` | Eval order for same-event hooks. **Lower runs first.** Ties preserve YAML order. |
| `enabled` | bool | `true` | Feature flag. `false` loads but never fires. |
| `tags` | list[str] | `[]` | Free-form grouping (surfaced in introspection APIs). |

Scopes:

- `runtime.hooks[]` → fires on every agent turn (app-wide).
- `agents[].hooks[]` → fires only for that agent's turns.
  Each is stamped with `agent_id` at compile time; runtime
  filter fires only for the matching agent.

```yaml
runtime:
  hooks:
    - id: app_wide_compact
      on: turn_start
      priority: 50              # runs before default-priority hooks
      condition: { type: context_pressure, threshold: 0.8 }
      action: { type: compact_context }

agents:
  - id: reviewer
    role: specialist
    brain: { ... }
    hooks:
      - id: reviewer_lint
        on: tool_end
        condition:
          type: all_of
          conditions:
            - { type: tool_name, match: "*.write" }
            - { type: tool_failed }
        action:
          type: notify
          title: "Reviewer caught a failed write"
          message: "{{tool.fqn}}: {{tool.error}}"
        max_fires: 10
        tags: [review, qa]
```

## The 15 events

Status legend: `live` = wired and emitted; `alias` = forwards to another event.

| Event | Status | Fires when | Context available |
|-------|:------:|------------|-------------------|
| `turn_start` | live | Start of each agent turn | turn, messages, tokens. |
| `turn_end` | live | End of each agent turn | turn, messages, tokens, tool_calls. |
| `tool_start` | live | Before a tool executes | tool_name, tool_params. |
| `tool_end` | live | After a tool executes | tool_name, tool_params, tool_result, tool_error. |
| `pre_tool_use` | alias | forwards to `tool_start`. | same as `tool_start`. |
| `post_tool_use` | alias | forwards to `tool_end`. | same as `tool_end`. |
| `user_prompt` | alias | forwards to `turn_start`. | same as `turn_start`. |
| `session_start` | live | First turn only (`turn == 0`) | messages, agent_id. |
| `session_end` | live | `manager.end_session` (DELETE /sessions, idle expiry) | `state._session_id`. |
| `pre_compact` | live | Before context compaction runs | messages, tokens. |
| `error` | live | LLM call failed | `state._error`, `state._error_code` (`rate_limit`, `context_overflow`, `billing`, `timeout`, `auth`, `network`, `internal`). |
| `approval_request` | live | `ApprovalQueue.enqueue` - before user is prompted | tool_name, tool_params, `state._approval_request`. |
| `agent_spawn` | live | `agent_spawn._run_agent` - before sub-agent starts | tool_params: `{agent_id, specialist, task}`. |
| `agent_complete` | live | `agent_spawn._run_agent` finally - after result available | tool_result: `{agent_id, specialist, task, status, errors, summary}`. |
| `activation` | declared | Background trigger routing only - declared, not yet wired at runtime | - |

Hooks declared with the `activation` event compile cleanly
(forward-compatible) but don't fire until wiring ships.

## The 14 conditions

`@register_condition` decorators.

### Composites (5)

| Condition | Behaviour |
|-----------|-----------|
| `always` | Fires every time. Useful with `cooldown` for periodic actions. |
| `never` | Kill-switch. Never fires. Lets you disable a hook without removing it. |
| `all_of` | All sub-conditions must match. Empty list = `true`. Short-circuits at first false. |
| `any_of` | At least one sub-condition matches. Empty list = `false`. Short-circuits at first true. |
| `not` | Negate one sub-condition. |

```yaml
condition:
  type: all_of
  conditions:
    - { type: tool_name, match: "filesystem.*" }
    - { type: tool_failed }
    - { type: turn_count, threshold: 3 }
```

```yaml
condition:
  type: any_of
  conditions:
    - { type: error_type, match: "billing" }
    - { type: error_type, match: "rate_limit" }
```

```yaml
condition:
  type: not
  condition: { type: tool_name, match: "memory.*" }
```

Unknown inner types evaluate to `false` (warning logged).

### Simple (9)

| Condition | Source | Purpose |
|-----------|--------|---------|
| `context_pressure` | | Token usage exceeds `threshold` (0.0-1.0, default 0.75). |
| `turn_count` | | At specific turn (`threshold`) or every N turns (`every`). |
| `tool_calls` | | Total tool count exceeds `threshold` (default 20). |
| `message_count` | | Message count exceeds `threshold` (default 50). |
| `tool_name` | | Current tool matches a pattern (fnmatch glob / list - NOT regex; `\|` for alternation). Tool events only. |
| `tool_failed` | | Last tool execution failed. `tool_end` / `post_tool_use` only. |
| `content_contains` | | Last 5 messages contain `keyword` (case-insensitive). |
| `error_type` | | Specific error code (supports wildcards). `error` event. |
| `expression` | | Sandboxed Python expr - vars: `turn`, `tools`, `messages`, `pressure`, `tokens`, `max_turns`. |

```yaml
# context_pressure
condition: { type: context_pressure, threshold: 0.75 }

# turn_count
condition: { type: turn_count, threshold: 10 }
condition: { type: turn_count, every: 5 }

# tool_name
condition: { type: tool_name, match: "Write|Edit" }
condition: { type: tool_name, match: "filesystem.*" }
condition: { type: tool_name, match: [Write, Edit, Insert] }

# error_type
condition: { type: error_type, match: "rate_limit*" }

# expression (sandboxed - no __builtins__, no imports)
condition: { type: expression, expr: "turn > 5 and pressure > 0.6" }
```

## The 13 general actions

`@register_action` decorators. Five additional
builder-specific actions (`compile_yaml`,
`auto_test_deploy`, `prefetch_ground_truth`,
`enforce_phase6`, `enforce_compile_fix`) ship with the same
file but are intended for builder apps.

### `compact_context`
Compact the message history to reduce token usage.

```yaml
action:
  type: compact_context
  strategy: summarize         # "summarize" (LLM) | "truncate" (fast)
  keep_recent: 10
  summary_max_tokens: 1024
  summary_prompt: |           # optional override
    Summarize the conversation so far...
  target_pressure: 0.5        # compact until pressure drops below this
  cooldown_turns: 3
```

### `inject_message`
Inject content the LLM is guaranteed to see on the next turn.

```yaml
action:
  type: inject_message
  strategy: auto              # auto | system | user | new_message
  content: "Remember to follow the coding standards."
  role: user                  # only used when strategy: new_message
  position: before_last       # only used when strategy: new_message
```

Strategies:

- `auto` (default) / `user` - appends to the last user
  message. Always visible, max compatibility.
- `system` - appends to the system prompt (creates one if
  none).
- `new_message` - separate message. Can break user /
  assistant alternation on strict providers - use sparingly.

### `module_action`
Execute any module action via the context_builder.

```yaml
action:
  type: module_action
  module: memory
  action: remember
  action_params:
    content: "{{tool.params.path}}"
```

### `module_action_inject`
Execute a module action and inject its result as a system
message - for real-time feedback (LSP diagnostics after
edits).

```yaml
action:
  type: module_action_inject
  module: lsp
  action: diagnostics
  action_params:
    path: "{{tool.params.path}}"
  format: auto                # "auto" (only on errors) | "always"
  prefix: "[Lint] "
```

### `log`
```yaml
action:
  type: log
  message: "Turn {turn}: {tokens} tokens, {tools} tool calls"
  level: info                 # debug | info | warning | error
```

### `shell`
Execute a shell command. Templates resolved.

```yaml
action:
  type: shell
  command: "python -m py_compile {{tool.params.path}}"
  cwd: "{{workspace}}"
  timeout: 30
  on_error: ignore            # ignore | inject
```

### `gate`
Block tool execution. `tool_start` / `pre_tool_use` only.

```yaml
action:
  type: gate
  reason: "Direct file deletion is not allowed in this project"
```

When a gate fires, the tool is **not executed** and the
agent receives an error explaining why.

### `transform_params`
Modify tool parameters before execution. `tool_start` only.

```yaml
action:
  type: transform_params
  transformation:
    set:
      timeout: 60
      encoding: "utf-8"
    remove: [dangerous_flag]
```

### `transform_result`
Modify tool result after execution. `tool_end` only.

```yaml
action:
  type: transform_result
  transformation:
    append_to_result: "\nRemember to run tests after editing."
    inject_note: "File was modified - consider running the test suite."
```

### `chain`
Run multiple actions in sequence.

```yaml
action:
  type: chain
  stop_on_failure: false      # if true, abort the chain on first error
  actions:
    - { type: log, message: "Edit detected on turn {turn}" }
    - { type: shell, command: "python -m py_compile {{tool.params.path}}" }
    - type: module_action_inject
      module: lsp
      action: diagnostics
      action_params: { path: "{{tool.params.path}}" }
```

Failed actions land in `state.metadata["hook_failures"]` as
`[{action, error}, ...]` - later actions (or the agent loop)
can inspect partial failures.

### `notify`
Send a notification to the client via the Socket.IO event
bus.

```yaml
action:
  type: notify
  title: "Context pressure high"
  message: "Token usage at {tokens} - compaction may be needed."
  level: warning              # info | warning | error
  tag: pressure_alert         # optional grouping tag
```

### `pipe`
Generic tool-chaining primitive. Routes the output of the
current tool into any other tool with field extraction.

```yaml
action:
  type: pipe
  to: lsp.notify_change       # destination - native module OR mcp.<server>.<tool>
  map:
    path: "{{tool.params.path}}"
    content: "{{tool.result.content}}"
  extra:                       # literal params, not templated
    force: true
  on_error: log                # ignore (default) | log | raise
```

### `lsp_diagnose`
Universal post-write LSP trigger. Reads the file path +
content from any write-shaped tool, calls
`lsp.notify_change`, and optionally injects the diagnostics
into the agent's next turn.

```yaml
action:
  type: lsp_diagnose
  path_field: ["path", "file_path"]    # try these param names in order
  content_field: ["content"]
  publish: true                          # push to preview "diagnostics" channel
  inject_result: true                    # include lint in tool result (self-correction loop)
  read_from_disk: true                   # fall back to disk read when content absent from params
```

Lets any module (filesystem, workspace, custom writers, MCP
tools) get free diagnostics via one YAML hook.

## Templating - `{{tool.*}}` placeholders

Used by `module_action`, `module_action_inject`, `pipe`,
`shell`. Helper primitives: `_walk_path` (jsonpath-lite
navigation) and `_render_tool_templates` (recursive
template resolution).

| Placeholder | Resolves to |
|-------------|-------------|
| `{{tool.name}}` / `{{tool.fqn}}` | The tool that fired the hook. |
| `{{tool.params.X}}` | `params[X]` - supports dotted paths + indices. |
| `{{tool.result.X}}` | Field of the tool's output - same syntax. |
| `{{tool.result}}` | Whole result, JSON-stringified. |
| `{{tool.error}}` | Error message or empty string. |

Path syntax (applies to both `params.X` and `result.X`):

- Dot-separated dict keys: `user.login`.
- Numeric segments = list index: `files.0.path`,
  `items.-1.id`.
- Combine: `response.hits.0.user.name`.
- Missing segments render as empty string (safe-navigation,
  never raises).

```yaml
# tool_context.tool_result:
#   {"user": {"login": "alice"}, "files": [{"path": "a.md"}, {"path": "b.md"}]}
text: "PR by {{tool.result.user.login}}, first file: {{tool.result.files.0.path}}"
# → "PR by alice, first file: a.md"
```

## Tool-chaining example pipelines

### 1. Auto-lint + notify on any MCP file write

```yaml
runtime:
  hooks:
    - id: lint_and_notify
      on: tool_end
      condition: { type: tool_name, match: "mcp_github.create_or_update_file" }
      action:
        type: chain
        actions:
          - type: lsp_diagnose
            path_field: [path]
            content_field: [content]
            inject_result: true       # agent sees lint errors → self-corrects
          - type: pipe
            to: mcp_slack.send_message
            map:
              channel: "#deploy"
              text: |
                {{tool.params.owner}}/{{tool.params.repo}} - {{tool.params.path}}
                commit: {{tool.result.commit.sha}}
```

### 2. Extract a nested array element + call another tool

```yaml
runtime:
  hooks:
    - id: search_to_notion
      on: tool_end
      condition: { type: tool_name, match: "mcp_search.elastic" }
      action:
        type: pipe
        to: notion.page.create
        map:
          title: "{{tool.result.hits.0.title}}"
          url:   "{{tool.result.hits.0.url}}"
          tags:  "{{tool.result.hits.0.metadata.tags}}"
```

### 3. Forward the entire result as JSON

```yaml
action:
  type: pipe
  to: archive.log
  map:
    message: "{{tool.name}} completed"
    payload: "{{tool.result}}"        # whole output, stringified JSON
```

### 4. Gate downstream on upstream error

```yaml
action:
  type: chain
  actions:
    - type: pipe
      to: ci.trigger_build
      map:
        commit_sha: "{{tool.result.commit.sha}}"
      on_error: raise                 # abort the chain on upstream failure
    - type: pipe
      to: slack.send_message
      map:
        channel: "#ci"
        text: "Build started for {{tool.result.commit.sha}}"
```

## Why this matters

- **Zero code** - pipelines are pure YAML.
- **MCP-ready** - works identically for native modules and
  MCP tools; same `tool_context` shape.
- **Composable** - wrap a `pipe` in a `chain` for
  multi-step workflows with per-step error control.
- **Safe** - missing fields render empty, never raise;
  pipelines degrade gracefully when upstream tools change
  shape.
- **Debuggable** - each `pipe` logs target + outcome; turn
  on `DIGITORN_LOGGING__LEVEL=debug` to see template
  resolutions.

## Builder-specific actions (5)

Same file but intended for builder apps - they encode a
plan-fix-deploy loop:

| Action | Source | Purpose |
|--------|--------|---------|
| `compile_yaml` | | Compile a YAML in `state` and persist `_state/compile.json`. |
| `auto_test_deploy` | | After deploy, run a smoke message + persist `_state/tests.json`. |
| `prefetch_ground_truth` | | Pre-load module / trigger / template / example catalogs into the system prompt. |
| `enforce_phase6` | | Turn-end guard: if a deploy happened but no successful smoke test, inject a reminder. |
| `enforce_compile_fix` | | Turn-end guard: if `_state/compile.json` shows errors AND no fix turn has run, inject a reminder. |

Don't use these in your own apps - they couple to the
Builder's state layout.

## Cross-references

- App-config block reference (`runtime.hooks` +
  `agents[].hooks`):
  [App Configuration → runtime.hooks](../../language/02-app-config.md#runtime---lifecycle-and-execution-policy)
- Behaviour engine (a different mechanism - fires per-tool
  via `pre_tool_check` / `post_tool_check`, configured under
  `security.behavior`):
  [Behavior Engine](../../language/43-behavior.md)
- Middleware (parallel pipeline that wraps every LLM call,
  not tool calls): [middleware.md](middleware.md)
- LSP module (target for `lsp_diagnose`):
  [lsp reference](../modules/lsp.md)
