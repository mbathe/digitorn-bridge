---
id: hooks
title: "Hooks V2 (runtime automation system)"
type: concept
keywords: [hooks, event, condition, action, turn_start, turn_end, tool_start, tool_end, pre_tool_use, post_tool_use, session_start, session_end, pre_compact, user_prompt, error, approval_request, agent_spawn, agent_complete, activation, context_pressure, turn_count, tool_calls, message_count, tool_name, tool_match, tool_failed, content_contains, error_type, expression, always, compact_context, inject_message, module_action, module_action_inject, log, shell, gate, transform_params, transform_result, chain, notify, cooldown]
related: [execution-modes, capabilities, brain-providers]
source: packages/digitorn/core/runtime/hooks.py
---

# Hooks V2 -- runtime automation system

## What it is

Hooks are **condition-action pairs** evaluated during the agent loop at specific events. When a condition is met, the corresponding action executes automatically. Hooks enable runtime automation without changing agent prompts: context compaction, tool interception, diagnostics injection, shell commands, notifications, and more.

Hooks work in all execution modes (conversation, one_shot, background).

## YAML reference

```yaml
execution:
  hooks:
    - id: unique_hook_id
      on: turn_end                   # Event to listen for
      condition:
        type: context_pressure       # Condition type
        threshold: 0.75              # Condition-specific params
      action:
        type: compact_context        # Action type
        strategy: summarize          # Action-specific params
        keep_recent: 10
      cooldown: 30                   # Min seconds between fires (0 = no cooldown)
```

## Events (15 total)

Events determine **when** the hook is evaluated:

| Event | When it fires | Use case |
|-------|---------------|----------|
| `turn_start` | Before the LLM generates a response | Inject context, check limits |
| `turn_end` | After the LLM response is processed | Compact context, log stats |
| `tool_start` | Before a tool executes | Log tool usage |
| `tool_end` | After a tool executes | Trigger diagnostics, log results |
| `pre_tool_use` | Before tool execution (can modify/block) | Gate, transform params |
| `post_tool_use` | After tool execution (can modify result) | Transform result, inject notes |
| `session_start` | When a session begins | Initialize state |
| `session_end` | When a session ends | Cleanup, report |
| `pre_compact` | Before context compaction | Save important state |
| `user_prompt` | When user sends a message | Filter input, inject context |
| `error` | When an error occurs | Error handling, retry |
| `approval_request` | When approval is needed | Auto-approve, notify |
| `agent_spawn` | When a sub-agent is spawned | Log, limit |
| `agent_complete` | When a sub-agent completes | Collect results |
| `activation` | When a background trigger fires | Filter, route |

## Conditions (10 types)

Conditions determine **whether** the hook fires:

### always

Fires every time the event occurs. Use with `cooldown` for periodic actions.

```yaml
condition:
  type: always
```

### context_pressure

Fires when estimated token usage exceeds a threshold.

```yaml
condition:
  type: context_pressure
  threshold: 0.75              # 0.0-1.0, default: 0.75
```

### turn_count

Fires at a specific turn or every N turns.

```yaml
condition:
  type: turn_count
  threshold: 10                # Fire at turn 10
  # OR
  every: 5                     # Fire every 5 turns
```

### tool_calls

Fires when total tool call count exceeds a threshold.

```yaml
condition:
  type: tool_calls
  threshold: 20                # Default: 20
```

### message_count

Fires when message count exceeds a threshold.

```yaml
condition:
  type: message_count
  threshold: 50                # Default: 50
```

### tool_name

Fires when the current tool matches a pattern. Only works with tool_start/tool_end/pre_tool_use/post_tool_use events.

```yaml
condition:
  type: tool_name
  match: "filesystem.*"        # Wildcard pattern
  # OR
  match: "Write|Edit"          # Pipe-separated alternatives
  # OR
  match: ["filesystem.write", "filesystem.edit"]  # List
```

### tool_match

Fires when the current tool matches a list of FQN names. Like `tool_name` but uses exact FQN matching.

```yaml
condition:
  type: tool_match
  tools: ["filesystem.edit", "filesystem.write"]
```

### tool_failed

Fires when the last tool execution failed.

```yaml
condition:
  type: tool_failed
```

### content_contains

Fires when recent message content contains a keyword (case-insensitive, checks last 5 messages).

```yaml
condition:
  type: content_contains
  keyword: "error"
```

### error_type

Fires when a specific error type occurs. Supports wildcards.

```yaml
condition:
  type: error_type
  match: "rate_limited"
  # OR
  match: "auth_*"
```

### expression

Fires when a Python expression evaluates to True. Available variables: `turn`, `tools`, `messages`, `pressure`, `tokens`, `max_turns`.

```yaml
condition:
  type: expression
  expr: "turn > 5 and pressure > 0.6"
```

## Actions (11 types)

Actions determine **what happens** when the hook fires:

### compact_context

Intelligently compact the message history to reduce token usage.

```yaml
action:
  type: compact_context
  strategy: summarize          # truncate or summarize (default: summarize)
  keep_recent: 10              # Messages to preserve (default: 10)
  summary_max_tokens: 1024     # Max tokens for the summary
  target_pressure: 0.5         # Compact until below this pressure
  cooldown_turns: 3            # Min turns between compactions
```

### inject_message

Inject content into the conversation that the LLM will see.

```yaml
action:
  type: inject_message
  content: "Remember to check your memory before continuing."
  strategy: auto               # auto, system, user, new_message
```

Strategies:
- `auto` / `user`: Appends to the last user message (most compatible)
- `system`: Appends to the system prompt
- `new_message`: Creates a separate message (may break alternation)

### module_action

Execute a module action via context_builder.

```yaml
action:
  type: module_action
  name: lsp.notify_change        # Module.action format
  action_params:
    path: "{{tool.params.path}}"  # Supports {{tool.*}} templates
```

Available template variables:
- `{{tool.name}}` -- tool FQN
- `{{tool.path}}` -- tool params.path shortcut
- `{{tool.params.X}}` -- any tool parameter

### module_action_inject

Like `module_action` but injects the result into the conversation. Designed for real-time feedback like LSP diagnostics.

```yaml
action:
  type: module_action_inject
  name: lsp.diagnostics
  action_params:
    path: "{{tool.params.path}}"
  format: auto                 # auto (only on errors) or always
  prefix: "[Lint] "
```

### log

Log a message (for debugging hooks).

```yaml
action:
  type: log
  message: "Turn {turn}: {tokens} tokens, {tools} tool calls"
  level: info                  # debug, info, warning, error
```

Template variables: `{turn}`, `{tokens}`, `{tools}`, `{messages}`, `{max_turns}`.

### shell

Execute a shell command.

```yaml
action:
  type: shell
  command: "echo 'Tool used: {{tool.name}} on {{tool.path}}' >> /tmp/audit.log"
  timeout: 30                  # Seconds (default: 30)
  inject_result: false         # Inject stdout as system message
  on_error: ignore             # ignore or inject
```

Supports `{{tool.*}}`, `{{turn}}`, `{{tokens}}` templates.

### gate

Block tool execution. Only works with `pre_tool_use` event.

```yaml
action:
  type: gate
  reason: "File deletion is not allowed in this app"
```

When the gate fires, the tool is NOT executed and the agent receives an error message.

### transform_params

Modify tool parameters before execution. Only works with `pre_tool_use` event.

```yaml
action:
  type: transform_params
  set:
    timeout: 30
    max_results: 100
  remove:
    - dangerous_flag
```

### transform_result

Modify tool result after execution. Only works with `post_tool_use` event.

```yaml
action:
  type: transform_result
  append_to_result: "\n[Note: This file was auto-formatted]"
  inject_note: "File was formatted by the linter"
```

### chain

Run multiple actions in sequence.

```yaml
action:
  type: chain
  stop_on_failure: false
  actions:
    - type: log
      params:
        message: "Tool {tools} called at turn {turn}"
    - type: module_action
      params:
        name: lsp.notify_change
        action_params:
          path: "{{tool.params.path}}"
    - type: notify
      params:
        title: "Edit complete"
        message: "File edited successfully"
```

### notify

Send a notification to the client via SSE.

```yaml
action:
  type: notify
  title: "Context Warning"
  message: "Context is 80% full, compaction imminent"
  level: warning               # info, warning, error
```

## Cooldown

Minimum seconds between consecutive fires of the same hook. Prevents rapid re-firing.

```yaml
cooldown: 30                   # Fire at most once every 30 seconds
cooldown: 0                    # No cooldown (fire every time)
```

## Examples

### Auto-compact on context pressure (default behavior)

```yaml
hooks:
  - id: auto_compact
    on: turn_end
    condition:
      type: context_pressure
      threshold: 0.75
    action:
      type: compact_context
      strategy: summarize
      keep_recent: 10
    cooldown: 30
```

### LSP diagnostics after file edits

```yaml
hooks:
  - id: lint_after_edit
    on: tool_end
    condition:
      type: tool_match
      tools: ["filesystem.edit", "filesystem.write"]
    action:
      type: module_action
      name: lsp.notify_change
      action_params:
        path: "{{tool.params.path}}"
    cooldown: 2
```

### Block file deletion

```yaml
hooks:
  - id: block_delete
    on: pre_tool_use
    condition:
      type: tool_name
      match: "filesystem.rm"
    action:
      type: gate
      reason: "File deletion is disabled. Ask the user to delete files manually."
```

### Audit log via shell

```yaml
hooks:
  - id: audit_trail
    on: tool_end
    condition:
      type: always
    action:
      type: shell
      command: "echo '{{tool.name}}: {{tool.params.path}}' >> /tmp/agent-audit.log"
    cooldown: 0
```

### Periodic reminder injection

```yaml
hooks:
  - id: memory_reminder
    on: turn_start
    condition:
      type: turn_count
      every: 10
    action:
      type: inject_message
      content: "Check your memory and todos before continuing. Stay on track."
```

### Chain: log + notify + compact

```yaml
hooks:
  - id: pressure_response
    on: turn_end
    condition:
      type: expression
      expr: "pressure > 0.8"
    action:
      type: chain
      actions:
        - type: log
          params:
            message: "High pressure at turn {turn}: {tokens} tokens"
        - type: notify
          params:
            title: "Context pressure high"
            message: "Compacting context..."
            level: warning
        - type: compact_context
          params:
            strategy: summarize
            keep_recent: 8
    cooldown: 60
```

### Transform params: force read-only shell

```yaml
hooks:
  - id: force_readonly_shell
    on: pre_tool_use
    condition:
      type: tool_name
      match: "shell.bash"
    action:
      type: transform_params
      set:
        timeout: 30
      remove:
        - run_in_background
```
