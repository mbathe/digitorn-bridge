---
id: tool-hooks
---

# Hooks

Hooks are declarative `condition → action` pairs that fire on
runtime events (turn boundaries, tool calls, errors, agent
spawns, ...). They sit between the agent loop and the tool
dispatcher and can mutate, gate, log, or redirect what the agent
does.

Two scopes:

| Scope | Where declared | Fires for |
|-------|----------------|-----------|
| **App-level** | `runtime.hooks[]` | Every agent in the app. |
| **Per-agent** | `agents[].hooks[]` | Only when that specific agent is the active turn. Merged with app-level. |

Every behaviour and field on this page maps to real code; entries
are cited with file + line.

## Quick example

```yaml
runtime:
  hooks:
    - id: lint_after_write
      "on": tool_end                # YAML quoting required (see below)
      condition:
        type: tool_name
        match: "filesystem.write"
      action:
        type: lsp_diagnose
        path_field: tool.params.path
        publish: true
        inject_result: true
      cooldown: 0
      max_fires: 0
      priority: 100
      enabled: true
      tags: [code-quality]
```

## YAML 1.1 `on` quoting - critical

YAML 1.1 parses **unquoted `on` as the boolean `True`**. Always
quote the field:

```yaml
- id: my_hook
  "on": tool_end           # OK
  on: tool_end             # WRONG: parses as boolean, schema rejects
```

The `HookConfig._validate_on` validator
catches the boolean case explicitly and raises a clear error
pointing at the unquoted `on`.

## `HookConfig` reference

(`extra: forbid`).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | string | *required* | Unique hook identifier. |
| `on` | string | `"turn_end"` | One of the events listed below (11 canonical + 3 aliases + 1 declared-only). **Must be quoted in YAML.** |
| `condition` | `HookConditionConfig` | *required* | Condition that must be true for the action to fire. |
| `action` | `HookActionConfig` | *required* | Action to execute when the condition matches. |
| `cooldown` | float | `0.0` | Minimum seconds between fires (0 = no cooldown). |
| `max_fires` | int ≥ 0 | `0` | Cap total fires per app lifetime. `0` = unlimited. |
| `priority` | int | `100` | Evaluation order among hooks on the same event - lower runs first. Same priority → YAML order. |
| `enabled` | bool | `true` | Feature flag. `false` = parsed but never fires. |
| `tags` | list[string] | `[]` | Free-form tags for introspection. Not used by the runtime. |

## The events

`_HOOK_EVENTS` holds 15
identifiers: **11 canonical events**, **3 aliases** that resolve
to canonical names, and **1 declared-only** event not yet wired
at the hook layer. Hooks fire on exactly one of:

| Event | When |
|-------|------|
| `turn_start` (alias `user_prompt`) | Beginning of a turn, after the user input is received. |
| `turn_end` | End of a turn, after the LLM emits no more tool calls. |
| `tool_start` (alias `pre_tool_use`) | Before a tool call dispatch. |
| `tool_end` (alias `post_tool_use`) | After a tool call completes (success or failure). |
| `session_start` | First turn of a session (`turn == 0`). |
| `session_end` | When the session is closed (graceful or abort). |
| `pre_compact` | Right before context compaction. |
| `error` | An exception escapes the agent loop. |
| `approval_request` | An approval gate (`tools.capabilities.approve`) enqueues a request. |
| `agent_spawn` | A sub-agent is spawned via `agent_spawn.agent`. |
| `agent_complete` | A sub-agent finishes (success or failure). |
| `activation` | Background trigger / channel activation routes to the app. (Declared-only - not yet routed at the hook layer in current builds.) |

The aliases (`pre_tool_use`/`post_tool_use`, `user_prompt`)
resolve to the canonical events.

## Conditions (14 built-in)

Registered via `@register_condition` decorators. Conditions get
a `TurnState` snapshot and return `True` to fire the action.

| Condition | Source | Params |
|-----------|--------|--------|
| `always` | | (none) - always fires. |
| `never` | | (none) - useful for temporarily disabling without editing YAML. |
| `context_pressure` | | `threshold: float` (default `0.75`). Fires when the token usage ratio crosses the threshold. |
| `turn_count` | | `threshold: int` (required), `every: int` (optional). Fires AT or EVERY N turns. |
| `tool_calls` | | `threshold: int`. Fires when the cumulative tool-call count for the turn crosses the threshold. |
| `message_count` | | `threshold: int`. Fires when the conversation message count crosses the threshold. |
| `tool_name` | | `match: str \| list[str]`. fnmatch glob (NOT regex). Use `\|` for alternation, `*` for wildcards. Compiler validates each pattern against known tools. |
| `tool_failed` | | (none). Fires when the active tool call returned `success: false`. Use with `tool_end`. |
| `content_contains` | | `keyword: str`. Matches the LLM's response or the user's message. |
| `error_type` | | `match: str` (regex). Matches the exception type / message. Use with `error`. |
| `expression` | | `expr: str`. A Python-like expression evaluated against the turn state. |
| `all_of` | | `conditions: list`. AND of nested conditions, short-circuit. |
| `any_of` | | `conditions: list`. OR of nested conditions, short-circuit. |
| `not` | | `condition: dict`. Negates a nested condition. |

Composite operators nest freely:

```yaml
condition:
  type: all_of
  conditions:
    - { type: tool_name, match: "filesystem.write" }
    - type: not
      condition: { type: tool_failed }
```

## Actions (15 built-in)

Registered via `@register_action` decorators. The first 13 are
general-purpose; the last 2 (`compile_yaml`, `auto_test_deploy`)
are scoped to the builder app and not intended for end-user YAMLs.
Actions receive the turn state plus the firing event payload.

| Action | Source | Params |
|--------|--------|--------|
| `compact_context` | | `strategy: str` (`truncate \| summarize`), `keep_last: int`. |
| `inject_message` | | `content: str` (required), `role: str` (default `user`), `placeholder: str` (optional). Injects a message into the conversation. |
| `module_action` | | `module: str`, `action: str` (required), `params: dict` (or `action_params`). Calls a module action - fire-and-forget. |
| `module_action_inject` | | Same as `module_action` plus `role: str`. The action's result is injected back as a message. |
| `log` | | `message: str` (required), `level: str` (default `info`). Writes to the daemon log. |
| `shell` | | `command: str` (required), `cwd`, `timeout`, `on_error`. Runs a shell command with `{{tool.*}}` template support. |
| `gate` | | `reason: str`, `allow: bool`. **Blocks the in-flight tool call** when `allow: false`. Use with `tool_start`. |
| `transform_params` | | `transformation: dict`. Modifies the tool params before execution. Use with `tool_start`. |
| `transform_result` | | `transformation: dict`. Modifies the tool result before it's returned to the agent. Use with `tool_end`. |
| `chain` | | `actions: list`. Run multiple actions sequentially. Each action sees the previous one's output. |
| `notify` | | `title`, `message`, `level`, `tag`. Fires a UI notification (Socket.IO event). |
| `pipe` | | `to: str` (required), `map: dict`, `extra: dict`, `on_error`. Routes the current tool's output into another tool. |
| `lsp_diagnose` | | `path_field`, `content_field`, `publish: bool`, `inject_result: bool`, `read_from_disk: bool`. Universal post-write LSP trigger. Reads `{{tool.params.path}}` + content, calls `lsp.notify_change`, optionally injects diagnostics back into the loop. |
| `compile_yaml` | | YAML compile + state write. Used by the builder app. |
| `auto_test_deploy` | | Auto-deploy + smoke test. Used by the builder app. |

## Templating in actions

`module_action`, `module_action_inject`, `pipe`, and `shell` apply
**template resolution** to their params automatically. The
following placeholders are available inside hook actions:

| Placeholder | Meaning |
|-------------|---------|
| `{{tool.name}}` | The current tool's full name. |
| `{{tool.params.X}}` | A field from the tool's params (dotted access supported). |
| `{{tool.params.X.0.y}}` | Array indexing. |
| `{{tool.result.X}}` | A field from the tool's result. |
| `{{tool.result}}` | The whole result, as JSON. |
| `{{tool.error}}` | The error message (when `tool_failed`). |

The walker is at  and the template renderer
is at . Both apply automatically
to the four templating actions; no explicit opt-in is needed.

## Two flagship patterns

### Auto-lint after every write (`lsp_diagnose`)

```yaml
runtime:
  hooks:
    - id: auto_lint
      "on": tool_end
      condition:
        type: tool_name
        match: "filesystem.write|workspace.write"
      action:
        type: lsp_diagnose
        path_field: tool.params.path
        content_field: tool.params.content
        publish: true             # push to the diagnostics preview channel
        inject_result: true       # merge lint into the tool result
        read_from_disk: false     # content comes from params
```

The `lsp_diagnose` action automates the most common post-write
chore: any module that writes a file (filesystem, workspace,
custom writer, or an MCP tool) gets free linting + diagnostics
publication via one declarative hook.

### Tool chaining (`pipe`)

```yaml
runtime:
  hooks:
    - id: web_fetch_to_summary
      "on": tool_end
      condition:
        type: all_of
        conditions:
          - { type: tool_name, match: "web.fetch" }
          - type: not
            condition: { type: tool_failed }
      action:
        type: pipe
        to: web.extract              # send fetch's output into extract
        map:
          html: "{{tool.result.text}}"
          schema: "links"
        extra:
          max_links: 10
        on_error: log                # log | ignore | raise
```

`pipe` is the generic tool-chaining primitive - it routes any
tool's output into any other tool with field mapping and template
resolution.

## Composite conditions - short-circuit

`all_of`, `any_of`, `not` are short-circuit operators. They nest
freely:

```yaml
condition:
  type: any_of
  conditions:
    - type: all_of
      conditions:
        - { type: tool_name, match: "shell.bash" }
        - { type: content_contains, keyword: "rm -rf" }
    - type: tool_failed
action:
  type: notify
  level: error
  message: "Suspicious or failed tool call: {{tool.name}}"
```

## Per-agent hooks vs app hooks

App hooks (`runtime.hooks[]`) fire for **every agent** on the
matching event. Per-agent hooks (`agents[].hooks[]`) fire **only
when that specific agent** is the active turn - useful for
specialist-only behaviour (e.g. a `reviewer` agent that runs
extra lint, a `writer` agent that logs every edit). App hooks
still fire for every agent; the per-agent ones add on top.

```yaml
agents:
  - id: reviewer
    role: specialist
    hooks:
      - id: log_reviewer_edits
        "on": tool_end
        condition: { type: tool_name, match: "filesystem.edit" }
        action:
          type: log
          level: info
          message: "Reviewer edited {{tool.params.path}}"
```

## Cooldowns and max-fires

- **`cooldown`** - minimum seconds between fires. Useful when a
  hook would otherwise spam (e.g. a watcher firing every
  `tool_end` when the agent is in a tight tool-call loop).
- **`max_fires`** - total fires per app lifetime
  (across all sessions). `0` = unlimited. Useful for one-shot
  setup hooks (`session_start` + `module_action` to bootstrap
  state) or to bound a pathological hook that's still being
  tuned.

## Compile-time validation

The `HookConfig` schema (`extra: forbid`) catches:

- Unknown event names - the validator
  emits a "Did you mean" suggestion.
- Unquoted `on` parsed as boolean - explicit error pointing at
  the YAML quoting issue.
- Missing `id` / `condition` / `action`.
- Negative `cooldown`, `max_fires`, or non-integer `priority`.

The condition / action dispatch (registered names) is
validated at hook-engine init - typos in `condition.type` or
`action.type` raise a clear error pointing at the bad hook.

## Extending the registry

 and `register_condition` are public
functions - third-party code can register custom conditions and
actions:

```python
from digitorn.core.runtime.hooks import register_condition, register_action

@register_condition("our_custom", params={"window": "required"})
def _eval_our_custom(state, params):
    return state.something_for_window(params["window"])

@register_action("our_action", params={"target": "required"})
async def _exec_our_action(rt, state, hook, event_payload):
    target = hook.action.params["target"]
    ... # do the work
```

Once registered, custom conditions and actions are usable in YAML
exactly like the built-ins.

## Cross-references

- App-config block reference (`runtime.hooks`,
  `agents[].hooks`):
  [App Configuration](02-app-config.md)
- Compaction integration (auto-injected `compact_context` hook):
  [Context Management → Token pressure and the auto-compact hook](06-context-management.md#token-pressure-and-the-auto-compact-hook)
- Hooks vs middleware (different timing, different scope):
  [Middleware Pipeline](17-middleware.md)
- Capabilities (gates fire as part of `tool_start`):
  [Security](11-security.md)
- LSP diagnostics:
  [LSP Diagnostics](27-lsp.md)
