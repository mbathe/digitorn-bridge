---
id: hooks
title: Hooks V2
sidebar_label: Hooks
sidebar_position: 6
description: Condition-action hooks that fire during the agent loop — 15 events, 10 conditions, 11 actions.
---

# Hooks V2

Hooks are condition-action pairs that fire at specific points in the agent loop.
They allow you to control agent behavior declaratively in YAML without writing
code.

## Overview

```yaml
execution:
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
- **`on`** — the event that triggers evaluation
- **`condition`** — when to fire (evaluated every time the event occurs)
- **`action`** — what to do when the condition is met
- **`cooldown`** — minimum seconds between firings (optional)

---

## Hook schema (full)

Every hook in `execution.hooks` or `agents[].hooks` accepts:

| Field | Type | Default | Purpose |
|---|---|---|---|
| `id` | string | **required** | Unique within its scope (execution or agent). |
| `on` | string | `"turn_end"` | Event name — see [Events](#events) (15). Aliases: `pre_tool_use`, `post_tool_use`, `user_prompt` resolve to `tool_start`, `tool_end`, `turn_start`. |
| `condition` | object | `{type: always}` | One of 14 condition types (simple + composite). See [Conditions](#conditions-14). |
| `action` | object | **required** | One of 13 action types. See [Actions](#actions-13). |
| `cooldown` | float (s) | `0` | Min seconds between fires. |
| `max_fires` | int | `0` | Lifetime cap on firings. `0` = unlimited. |
| `priority` | int | `100` | Evaluation order for same-event hooks. **Lower runs first.** Ties preserve YAML order. |
| `enabled` | bool | `true` | Feature flag. `false` loads but never fires. |
| `tags` | list[str] | `[]` | Free-form grouping tags; surfaced in introspection APIs. |

**Scopes** :
- Declare under `execution.hooks[]` → fires for every agent turn.
- Declare under `agents[].hooks[]` → fires only for that agent's turns (specialist hooks). Merged with execution-level hooks; priority is global.

```yaml
execution:
  hooks:
    - id: app_wide_compact
      on: turn_start
      priority: 50        # runs before default-priority hooks
      condition: {type: context_pressure, threshold: 0.8}
      action: {type: compact_context}

agents:
  - id: reviewer
    role: specialist
    hooks:
      - id: reviewer_lint
        on: tool_end
        condition:
          type: all_of
          conditions:
            - {type: tool_name, match: "*.write"}
            - {type: tool_failed}
        action:
          type: notify
          title: "Reviewer caught a failed write"
          message: "{{tool.fqn}}: {{tool.error}}"
        max_fires: 10
        tags: [review, qa]
```

## Events (15)

**Status column**: ✅ wired and emitted • 🔁 alias (resolves to canonical event) • ⚠️ accepted at compile-time, not yet emitted at runtime.

| Event | Status | When it fires | Context available |
|---|---|---|---|
| `turn_start` | ✅ | Start of each agent turn | turn, messages, tokens |
| `turn_end`   | ✅ | End of each agent turn | turn, messages, tokens, tool_calls |
| `tool_start` | ✅ | Before a tool executes | tool_name, tool_params |
| `tool_end`   | ✅ | After a tool executes | tool_name, tool_params, tool_result, tool_error |
| `pre_tool_use` | 🔁 | Alias → `tool_start` | same as `tool_start` |
| `post_tool_use` | 🔁 | Alias → `tool_end` | same as `tool_end` |
| `user_prompt` | 🔁 | Alias → `turn_start` | same as `turn_start` |
| `session_start` | ✅ | First turn only (turn == 0) | messages, agent_id |
| `pre_compact` | ✅ | Before context compaction runs | messages, tokens |
| `error`      | ✅ | LLM call failed | `state._error`, `state._error_code` (rate_limit, context_overflow, billing, timeout, auth, network, internal) |
| `session_end` | ✅ | `manager.end_session` (DELETE /sessions, idle expiry) | `state._session_id` |
| `approval_request` | ✅ | `ApprovalQueue.enqueue` — before the user is prompted | tool_name, tool_params, `state._approval_request` |
| `agent_spawn` | ✅ | `agent_spawn._run_agent` — before the sub-agent starts | tool_params: `{agent_id, specialist, task}` |
| `agent_complete` | ✅ | `agent_spawn._run_agent` finally — after result available | tool_result: `{agent_id, specialist, task, status, errors, summary}` |
| `activation` | ⚠️ | Not yet emitted — background-trigger routing only | — |

Hooks declared with a ⚠️ event **compile cleanly** and can be shipped in apps; they simply don't fire until the corresponding runtime wiring ships. Forward-compatible.

---

## Conditions (14)

10 simple conditions + 4 composite (`all_of`, `any_of`, `not`, `never`). All are sync-evaluated and return a single boolean.

### always

Fires every time the event occurs. Useful with `cooldown` for periodic actions.

```yaml
condition:
  type: always
```

### never

Kill-switch. Never fires. Lets you temporarily disable a hook without
removing its definition (e.g. during an incident).

```yaml
condition:
  type: never
```

### all_of / any_of / not — composite conditions

Build complex logical predicates by nesting condition dicts:

```yaml
# Fire only when a filesystem write FAILS after turn 3
condition:
  type: all_of
  conditions:
    - {type: tool_name, match: "filesystem.*"}
    - {type: tool_failed}
    - {type: turn_count, threshold: 3}
```

```yaml
# Fire on EITHER billing or rate-limit errors
condition:
  type: any_of
  conditions:
    - {type: error_type, match: "billing"}
    - {type: error_type, match: "rate_limit"}
```

```yaml
# Fire on every tool call EXCEPT memory operations
condition:
  type: not
  condition:
    type: tool_name
    match: "memory.*"
```

**Semantics**:
- `all_of` with empty list = `true` (vacuously).
- `any_of` with empty list = `false`.
- Short-circuits: `all_of` stops at first false, `any_of` at first true.
- Unknown inner types evaluate to `false` (warning logged).
### context_pressure

Fires when estimated token usage exceeds a ratio of the context window.

```yaml
condition:
  type: context_pressure
  threshold: 0.75          # 0.0-1.0 (default: 0.75)
```
### turn_count

Fires at a specific turn number or every N turns.

```yaml
# Fire at turn 10
condition:
  type: turn_count
  threshold: 10

# Fire every 5 turns
condition:
  type: turn_count
  every: 5
```
### tool_calls

Fires when the total tool call count exceeds a threshold.

```yaml
condition:
  type: tool_calls
  threshold: 20            # default: 20
```
### message_count

Fires when the message count exceeds a threshold.

```yaml
condition:
  type: message_count
  threshold: 50            # default: 50
```
### tool_name

Fires when the current tool matches a pattern. Only works with tool events
(`tool_start`, `tool_end`, `pre_tool_use`, `post_tool_use`).

```yaml
# Match specific tools
condition:
  type: tool_name
  match: "Write|Edit"

# Match with wildcards
condition:
  type: tool_name
  match: "filesystem.*"

# Match a list
condition:
  type: tool_name
  match:
    - Write
    - Edit
    - Insert
```
### tool_failed

Fires when the last tool execution failed. Only works with `tool_end` and
`post_tool_use` events.

```yaml
condition:
  type: tool_failed
```
### content_contains

Fires when recent messages contain a keyword (case-insensitive, checks last 5 messages).

```yaml
condition:
  type: content_contains
  keyword: "error"
```
### error_type

Fires when a specific error code occurs. Supports wildcards.

```yaml
condition:
  type: error_type
  match: "rate_limited"

condition:
  type: error_type
  match: "auth_*"
```
### expression

Fires when a Python expression evaluates to True. Available variables:
`turn`, `tools`, `messages`, `pressure`, `tokens`, `max_turns`.

```yaml
condition:
  type: expression
  expr: "turn > 5 and pressure > 0.6"
```
> **Note**: `eval` is sandboxed — `__builtins__` is not accessible from the
> expression, so imports, `open`, `exec`, etc. are unavailable. Only the
> listed variables and standard arithmetic/boolean operators work.

---

## Tool chaining — the runtime primitive

A single hook can route the **output** of any tool (native module or MCP
server) into the **input** of another tool — with field extraction, JSON
path navigation, and error handling. This is what turns Digitorn hooks
into a real workflow engine.

### Placeholders

Every action's string / dict / list params is scanned recursively and
any `{{...}}` placeholder resolved against the current `tool_context`:

| Placeholder | Resolves to |
|---|---|
| `{{tool.name}}` / `{{tool.fqn}}` | Name of the tool that fired the hook |
| `{{tool.params.X}}` | `params[X]` — supports dotted paths + indices |
| `{{tool.result.X}}` | Field of the tool's output — same syntax |
| `{{tool.result}}` | Whole result serialized as JSON |
| `{{tool.error}}` | Error message or empty string |

**Path syntax** (applies to both `params.X` and `result.X`):

- Dot-separated dict keys: `user.login`
- Numeric segments = list index: `files.0.path`, `items.-1.id`
- Combines: `response.hits.0.user.name`
- Missing segments render as empty string (safe-navigation — never raises)

Example:

```yaml
# tool_context.tool_result looks like:
#   {"user": {"login": "alice"}, "files": [{"path": "a.py"}, {"path": "b.py"}]}
text: "PR by {{tool.result.user.login}}, first file: {{tool.result.files.0.path}}"
# → "PR by alice, first file: a.py"
```

### The `pipe` action — clean API for chaining

```yaml
hooks:
  - event: tool_end
    condition:
      type: tool_name
      value: ["mcp.github.get_pull_request"]
    action:
      type: pipe
      to: mcp.slack.send_message
      map:
        channel: "#dev"
        text: "PR #{{tool.result.number}} — {{tool.result.title}} by {{tool.result.user.login}}"
      extra:
        as_user: true         # literal value — no templating
      on_error: log            # ignore (default) | log | raise
```

Params:

- `to` (required) — destination tool name, e.g. `module.action` or
  `mcp.<server>.<tool>`.
- `map` (dict) — destination param → template reference. Templating runs
  on every value in the tree (nested dicts/lists supported).
- `extra` (dict) — literal params merged into the final call. Useful for
  booleans / constants that shouldn't go through templating.
- `on_error` — behaviour when the downstream tool fails:
  - `"ignore"` (default) — swallow silently, log at debug level.
  - `"log"` — emit a warning-level log entry.
  - `"raise"` — propagate the error, abort the enclosing `chain` if any.

### Why this is powerful

- **Zero code** — new pipelines are pure YAML.
- **MCP-ready** — works identically for native modules and MCP tools; the
  `tool_context` shape is the same on both paths.
- **Composable** — wrap a `pipe` in a `chain` to get multi-step
  workflows with per-step error control.
- **Safe** — missing fields render empty, never raise; pipelines degrade
  gracefully when upstream tools change their response shape.
- **Debuggable** — each `pipe` logs the target + outcome; turn on
  `DIGITORN_LOGGING__LEVEL=debug` to see template resolutions.

### Example pipelines

**1. Auto-lint + notify on any MCP file write**

```yaml
hooks:
  - event: tool_end
    condition:
      type: tool_name
      value: ["mcp.github.create_or_update_file"]
    action:
      type: chain
      actions:
        - type: lsp_diagnose
          path_field: ["path"]
          content_field: ["content"]
          inject_result: true        # agent sees lint errors → self-corrects
        - type: pipe
          to: mcp.slack.send_message
          map:
            channel: "#deploy"
            text: |
              {{tool.params.owner}}/{{tool.params.repo}} — {{tool.params.path}}
              commit: {{tool.result.commit.sha}}
```

**2. Extract a nested array element and call another tool**

```yaml
hooks:
  - event: tool_end
    condition:
      type: tool_name
      value: ["mcp.search.elastic"]
    action:
      type: pipe
      to: notion.page.create
      map:
        title: "{{tool.result.hits.0.title}}"
        url:   "{{tool.result.hits.0.url}}"
        tags:  "{{tool.result.hits.0.metadata.tags}}"
```

**3. Forward the entire result as a JSON string**

```yaml
action:
  type: pipe
  to: archive.log
  map:
    message: "{{tool.name}} completed"
    payload: "{{tool.result}}"   # whole output, stringified JSON
```

**4. Gate downstream on upstream error**

```yaml
action:
  type: chain
  actions:
    - type: pipe
      to: ci.trigger_build
      map:
        commit_sha: "{{tool.result.commit.sha}}"
      on_error: raise       # abort the chain
    - type: pipe
      to: slack.send_message
      map:
        channel: "#ci"
        text: "Build started for {{tool.result.commit.sha}}"
```

### Templates also work in

- `module_action.action_params`
- `module_action_inject.action_params`
- `pipe.map`
- `shell.command`

All use the same resolver — no divergence between actions.

## Actions (13)

### compact_context

Compact the message history to reduce token usage.

```yaml
action:
  type: compact_context
  strategy: summarize       # "summarize" (uses LLM) or "truncate" (fast)
  keep_recent: 10           # Messages to preserve
  summary_max_tokens: 1024  # Max tokens for summary
  summary_prompt: |         # Custom summarization prompt (optional)
    Summarize the conversation so far...
  target_pressure: 0.5      # Compact until below this
  cooldown_turns: 3         # Min turns between compactions
```
Params:
- `strategy` — `"summarize"` (uses LLM) or `"truncate"` (fast, no LLM call). Default: `"summarize"`.
- `keep_recent` — Number of most recent messages to keep untouched. Default: `10`.
- `summary_max_tokens` — Max tokens budgeted for the LLM summary. Default: `1024`.
- `summary_prompt` — Optional custom prompt string for the summarizer. When
  omitted, a sensible default is used.
- `target_pressure` — Compact until token pressure drops below this. Default: `0.5`.
- `cooldown_turns` — Minimum turns between consecutive compactions. Default: `3`.

### inject_message

Inject content the LLM is guaranteed to see on the next turn.

```yaml
action:
  type: inject_message
  strategy: auto            # auto | system | user | new_message
  content: "Remember to follow the coding standards."
  role: user                # only used when strategy: new_message
  position: before_last     # only used when strategy: new_message
```
Params:
- `content` — Text to inject. Required.
- `strategy` — How the content is delivered. Default: `auto`.
  - `auto` (default) — appends to the last user message (most compatible
    with all providers; always visible).
  - `system` — appends to the existing system prompt (creates one if none
    exists).
  - `user` — same as `auto`: appends to the last user message.
  - `new_message` — creates a separate new message. May break
    user/assistant alternation on strict providers — use only when you need
    a standalone turn.
- `role` — Only used when `strategy: new_message`. `"user"` or `"system"`.
  Default: `"user"`.
- `position` — Only used when `strategy: new_message`. `"before_last"` or
  `"end"`. Default: `"before_last"`.

### module_action

Execute any module action via context_builder.

```yaml
action:
  type: module_action
  name: "memory.remember"
  action_params:
    key: "last_edit"
    value: "{{tool.path}}"
```
### module_action_inject

Execute a module action and inject its result as a system message.
Designed for real-time feedback (e.g., LSP diagnostics after edits).

```yaml
action:
  type: module_action_inject
  name: "lsp.diagnose"
  action_params:
    path: "{{tool.path}}"
  format: auto              # "auto" (only on errors) or "always"
  prefix: "[Lint] "
```
### log

Log a message. Useful for debugging hooks.

```yaml
action:
  type: log
  message: "Turn {turn}: {tokens} tokens, {tools} tool calls"
  level: info               # "debug", "info", "warning", "error"
```
### shell

Execute a shell command. Supports template variables.

```yaml
action:
  type: shell
  command: "python -m py_compile {{tool.path}}"
  timeout: 30               # seconds (default: 30)
  inject_result: false       # Inject stdout as system message
  on_error: ignore           # "ignore" or "inject"
```
Template variables: `{{tool.name}}`, `{{tool.path}}`, `{{tool.params.<key>}}`,
`{{turn}}`, `{{tokens}}`.

### gate

Block tool execution. Only works with `pre_tool_use` event.

```yaml
action:
  type: gate
  reason: "Direct file deletion is not allowed in this project"
```
When a gate fires, the tool is NOT executed and the agent receives an error
message explaining why.

### transform_params

Modify tool parameters before execution. Only works with `pre_tool_use`.

```yaml
action:
  type: transform_params
  set:
    timeout: 60
    encoding: "utf-8"
  remove:
    - dangerous_flag
```
### transform_result

Modify tool result after execution. Only works with `post_tool_use`.

```yaml
action:
  type: transform_result
  append_to_result: "\nRemember to run tests after editing."
  inject_note: "File was modified — consider running the test suite."
```
### chain

Execute multiple actions in sequence.

```yaml
action:
  type: chain
  stop_on_failure: false    # if true, abort the chain on the first error
  actions:
    - type: log
      params:
        message: "Edit detected on turn {turn}"
    - type: shell
      params:
        command: "python -m py_compile {{tool.path}}"
    - type: module_action_inject
      params:
        name: "lsp.diagnose"
        action_params:
          path: "{{tool.path}}"
```
Params:
- `actions` — List of `{type, params}` action definitions to run in order.
- `stop_on_failure` — If `true`, the chain aborts on the first unknown
  action or raised exception. Default: `false` (the chain keeps going).

Failed actions are recorded in `state.metadata["hook_failures"]` as a list
of `{action, error}` entries, so later actions (or the agent loop) can
inspect partial failures.

### notify

Send a notification to the client via the event bus (Socket.IO `/events`
namespace).

```yaml
action:
  type: notify
  title: "Context pressure high"
  message: "Token usage at {tokens} — compaction may be needed."
  level: warning             # "info", "warning", "error"
```

### pipe

Route the output of the current tool into another tool. Supports field
extraction via `{{tool.result.path.0.field}}` placeholders. The **main
primitive for building YAML pipelines** — see [Tool chaining](#tool-chaining--the-runtime-primitive)
above for the full reference.

```yaml
action:
  type: pipe
  to: lsp.notify_change       # destination tool (native or MCP)
  map:
    path: "{{tool.params.path}}"
    content: "{{tool.result.content}}"
  extra:                       # literal params, not templated
    force: true
  on_error: log                # ignore | log | raise
```

### lsp_diagnose

Universal post-write LSP trigger. Automatically extracts path + content
from any writer (native or MCP), calls `lsp.notify_change`, and
publishes diagnostics to the `preview.diagnostics` channel.

```yaml
action:
  type: lsp_diagnose
  path_field: ["file_path", "path", "filepath", "filename"]  # cascade
  content_field: ["content", "contents", "body", "text"]      # cascade
  read_from_disk: true         # fallback when content absent from params
  inject_result: true          # merge lint into the tool's result (self-correction loop)
  publish: true                # push to the diagnostics preview channel
```

See also: [Voice transcription](voice_transcription.md),
[Preview module](PREVIEW.md), [LSP module](modules/lsp.md).

---

## Complete Examples

### Auto-compact on context pressure

```yaml
execution:
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
      cooldown: 120
```
### Block dangerous commands

```yaml
execution:
  hooks:
    - id: no-rm-rf
      on: pre_tool_use
      condition:
        type: tool_name
        match: "Bash"
      action:
        type: gate
        reason: "Shell commands are disabled in this mode"
```
### Lint after every file edit

```yaml
execution:
  hooks:
    - id: lint-on-edit
      on: post_tool_use
      condition:
        type: tool_name
        match: "Write|Edit|Insert"
      action:
        type: chain
        actions:
          - type: shell
            params:
              command: "ruff check {{tool.path}} --output-format=json"
              inject_result: true
          - type: log
            params:
              message: "Linted {{tool.path}} after edit"
```
### Periodic reminders

```yaml
execution:
  hooks:
    - id: reminder
      on: turn_end
      condition:
        type: turn_count
        every: 10
      action:
        type: inject_message
        content: "Reminder: always write tests for new functions."
```
### React to errors

```yaml
execution:
  hooks:
    - id: on-rate-limit
      on: error
      condition:
        type: error_type
        match: "rate_limited"
      action:
        type: notify
        title: "Rate limited"
        message: "Provider rate limit hit. Backing off."
        level: warning
```
---

## Extending Hooks

Conditions and actions are registered in two global registries exposed by
`packages/digitorn/core/runtime/hooks.py`:

- `@register_condition(name)` — decorates a function
  `(state: TurnState, params: dict) -> bool`.
- `@register_action(name)` — decorates an async function
  `(state: TurnState, params: dict, **kwargs) -> None`. Keyword args include
  `provider` (the LLM provider) and `context_builder` when available.

Register your own at import time (e.g. in a module's `on_start`):

```python
from digitorn.core.runtime.hooks import (
    register_condition, register_action, TurnState,
)

@register_condition("has_user_goal")
def _has_goal(state: TurnState, params: dict) -> bool:
    ctx = getattr(state, "_agent_context", None)
    return bool(ctx and getattr(ctx, "user_goal", None))

@register_action("persist_state")
async def _persist(state: TurnState, params: dict, **kwargs) -> None:
    path = params.get("path", "/tmp/state.json")
    ...  # write your state
```

Once registered, the new names are usable in any hook's `condition.type`
or `action.type` in YAML.
