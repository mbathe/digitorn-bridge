---
id: 31-tool-hooks
---

# Tool Hooks - Automate Actions Around Tool Calls

Tool hooks fire **before or after individual tool calls** (not turns). They enable patterns like:
- Run a linter after every file edit
- Log every shell command
- Validate params before a dangerous tool executes

## Quick Start

```yaml
execution:
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
This hook runs `lsp.notify_change()` after every `filesystem.edit` or `filesystem.write` - giving the agent automatic diagnostics.

## Hook Events

| Event | When | Use Case |
|-------|------|----------|
| `turn_start` | Before LLM is called | Inject context, check state |
| `turn_end` | After LLM responds | Compact context, log turn |
| **`tool_start`** | Before a tool executes | Validate params, block dangerous calls |
| **`tool_end`** | After a tool completes | Run linter, log result, trigger follow-up |

## Condition: `tool_match`

Matches when the current tool call matches one of the listed patterns.

```yaml
condition:
  type: tool_match
  tools:
    - filesystem.edit          # Exact match
    - filesystem.write         # Exact match
    - filesystem.*             # Wildcard: any filesystem action
    - shell.run                # Exact match
```
**Wildcard support:** `module.*` matches all actions of a module.

## Template Variables

In every hook action's string / dict / list params, you can reference
the **input and output** of the tool that fired the hook:

| Variable | Returns |
|---|---|
| `{{tool.name}}` / `{{tool.fqn}}` | Tool name (e.g. `filesystem.edit`) |
| `{{tool.params.KEY}}` | Input param - supports dotted paths + array indices |
| `{{tool.result.KEY}}` | Output field - same syntax |
| `{{tool.result}}` | Whole result as JSON |
| `{{tool.error}}` | Error message, or `""` on success |

Paths navigate nested dicts and lists. Missing segments render as empty
strings - no crashes when an MCP server changes its response shape.

```yaml
# Tool returned: {"user": {"login": "alice"}, "files": [{"path": "a.py"}]}
text: "PR by {{tool.result.user.login}} - {{tool.result.files.0.path}}"
# → "PR by alice - a.py"
```

**For pipelines, see [Tool chaining](../tool_chaining.md)** - the dedicated
runtime primitive that builds multi-step workflows (`pipe` action,
field extraction, error handling).
## Examples

### Auto-lint after every edit
```yaml
hooks:
  - id: lint_after_edit
    on: tool_end
    condition:
      type: tool_match
      tools: ["filesystem.edit", "filesystem.write", "filesystem.multi_edit", "filesystem.patch"]
    action:
      type: module_action
      name: lsp.notify_change
      action_params:
        path: "{{tool.params.path}}"
    cooldown: 2
```
### Log every shell command
```yaml
hooks:
  - id: log_shell
    on: tool_end
    condition:
      type: tool_match
      tools: ["shell.run", "shell.bash"]
    action:
      type: log
      message: "Shell: {tool.name} in turn {turn}"
      level: info
```
### Remind agent after database changes
```yaml
hooks:
  - id: remind_after_db_write
    on: tool_end
    condition:
      type: tool_match
      tools: ["database.execute_query", "database.sql"]
    action:
      type: inject_message
      role: system
      content: "Remember to verify your database changes with a SELECT query."
    cooldown: 10
```
## Built-in Conditions (10)

| Condition | Params | Description |
|-----------|--------|-------------|
| `context_pressure` | `threshold: 0.75` | Token usage exceeds threshold |
| `turn_count` | `threshold: 10` or `every: 5` | Turn count reached or periodic |
| `tool_calls` | `threshold: 20` | Tool call count reached |
| `message_count` | `threshold: 50` | Message count reached |
| `always` | - | Every time |
| `tool_name` | `match: "Write\|Edit"` | Current tool matches pattern (wildcards, lists) |
| `tool_failed` | - | Last tool execution failed |
| `content_contains` | `keyword: "error"` | Recent messages contain keyword |
| `error_type` | `match: "rate_limited"` | Specific error code (wildcards) |
| `expression` | `expr: "turn > 5 and pressure > 0.6"` | Python expression |

**Note:** `tool_match` is an alias for `tool_name` for backward compatibility.

## Built-in Actions (13)

| Action | Params | Description |
|--------|--------|-------------|
| `compact_context` | `strategy, keep_recent` | Compress message history |
| `inject_message` | `role, content, position` | Inject system/user message |
| `module_action` | `name, action_params` | Call any module action |
| `module_action_inject` | `name, action_params, format, prefix` | Call module action and inject result |
| `log` | `message, level` | Log for debugging |
| `shell` | `command, timeout, inject_result` | Execute shell command |
| `gate` | `reason` | Block tool execution (pre_tool_use only) |
| `transform_params` | `set, remove` | Modify tool params (pre_tool_use only) |
| `transform_result` | `append_to_result, inject_note` | Modify tool result (post_tool_use only) |
| `chain` | `actions: [...]` | Execute multiple actions in sequence |
| `notify` | `title, message, level` | Send notification to client via Socket.IO |
| **`pipe`** | `to, map, extra, on_error` | **Route tool output → another tool.** See [Tool chaining](../tool_chaining.md). |
| **`lsp_diagnose`** | `path_field, content_field, inject_result, publish, read_from_disk` | **Universal post-write LSP.** Works for any writer (native + MCP). |

See [Hooks V2](../hooks.md) for full documentation of all conditions and actions.

### module_action_inject

Execute a module action and **inject its result into the conversation** as a system message. Unlike `module_action` which is fire-and-forget, this variant ensures the agent sees the action's output.

Designed for real-time feedback loops - e.g., LSP diagnostics after file edits.

```yaml
hooks:
  - id: lsp_after_edit
    on: tool_end
    condition:
      type: tool_match
      tools: ["filesystem.edit", "filesystem.write"]
    action:
      type: module_action_inject
      name: lsp.notify_change
      action_params:
        path: "{{tool.params.path}}"
      format: auto    # "auto" = only inject if errors/warnings exist
      prefix: ""      # optional text prefix
    cooldown: 2
```
**format options:**
- `auto` (default) - only inject if the result contains errors or warnings. Clean files produce no output.
- `always` - always inject the result, even if clean.

**Note:** For filesystem.edit and filesystem.write, LSP diagnostics are now integrated directly into the tool result (inline `lint` field). This hook type is still useful for custom feedback loops with other modules.
