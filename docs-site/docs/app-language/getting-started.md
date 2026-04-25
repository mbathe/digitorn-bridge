---

id: getting-started
title: Getting Started
sidebar_position: 1
format: md
---


# Getting Started

This guide walks you through creating and running your first LLMOS application.

## Prerequisites

- Python 3.11+
- LLMOS Bridge installed (`pip install llmos-bridge` or from source)
- An Anthropic API key (set `ANTHROPIC_API_KEY` environment variable)

## Your First App

Create a file called `hello.app.yaml`:

```yaml
app:
  name: hello
  version: "1.0"
  description: "My first LLMOS app"

agent:
  brain:
    provider: anthropic
    model: claude-sonnet-4-20250514
    temperature: 0
    max_tokens: 4096
  system_prompt: |
    You are a friendly assistant. Answer questions concisely.
  tools:
    - module: filesystem
      action: read_file
    - module: filesystem
      action: list_directory

triggers:
  - type: cli
    mode: conversation
    greeting: "Hello! I can read files for you. Ask me anything."
```

## Running the App

### Interactive mode (conversation)

```bash
llmos app run hello.app.yaml
```

This starts an interactive session. Type messages, get responses. Press `Ctrl+C` to exit.

### One-shot mode (single input)

```bash
llmos app run hello.app.yaml --input "List the files in the current directory"
```

The agent processes the input once and exits.

### Validate without running

```bash
llmos app validate hello.app.yaml
```

Checks YAML syntax, schema validation, and semantic consistency.

### What Validation Guarantees

The `AppCompiler` performs **7 layers** of validation at compile time — before any code runs:

1. **YAML syntax** — Valid YAML, correct structure
2. **Schema validation** (Pydantic) — Every field is type-checked:
   - `brain.temperature` must be 0-2, `max_tokens` must be 1-200000
   - `loop.type` must be one of `reactive`, `single_shot`, `continuous`
   - `security.profile` must be one of `readonly`, `local_worker`, `power_user`, `unrestricted`
   - Trigger types, approval fields, metric types — all validated as enums
3. **Semantic checks** — Cross-field consistency:
   - Agent/step IDs are unique (no duplicates)
   - `use:` references point to defined macro names
   - Static `goto:` targets reference existing step IDs
   - Tool definitions have `module`, `builtin`, or `id` (not both `module` and `builtin`)
4. **Module/action existence** (daemon mode) — When the daemon is running:
   - Every `module:` in `tools:` references an existing module
   - Every `action:` references an action that exists in that module
   - `exclude:` entries reference real actions
   - `capabilities.grant` and `capabilities.deny` modules are validated
5. **Agent ID cross-references** — Flow steps and macro bodies:
   - `agent: planner` in a flow step must match a defined agent ID
   - Works in nested structures (branches, loops, parallel, macros)
   - Dynamic references (`agent: "{{result.x}}"`) are skipped (resolved at runtime)
6. **Expression syntax** — Template expressions are pre-checked:
   - Unknown filters (`{{name | typo_filter}}`) produce warnings
   - Unmatched brackets (`{{name}` without closing `}}`) produce warnings
   - All 30 built-in filters are validated: `upper`, `lower`, `join`, `startswith`, `endswith`, `matches`, `split`, `filter`, `map`, `sort`, etc.
7. **Macro reference validation** — `use:` in flow steps must reference a defined macro name

**If validation passes, the app structure is guaranteed correct.** Runtime errors can still occur (e.g., a file doesn't exist, an API is down, or the LLM generates an unexpected response), but the app *definition itself* will not have structural issues.

### What Can Only Be Caught at Runtime

Some checks require live data and cannot be done at compile time:

- **Template expression results** — `{{result.step1.field}}` depends on what step1 returns
- **Dynamic dispatches** — `dispatch: { module: "{{result.x}}" }` resolved at runtime
- **File/path existence** — Memory paths, workspace paths
- **LLM responses** — What the model generates is unpredictable
- **External services** — API availability, database connections

These are handled by the error handling config (`on_tool_error`, `retry`, `on_error`) and the 15-step security pipeline.

```bash
# Quick validation for CI/CD pipelines
llmos app validate my-app.app.yaml && echo "App is valid"
```

## How It Works

1. **Compile** — The `AppCompiler` parses the YAML, validates it against the schema (Pydantic models), and checks semantic consistency (unique IDs, valid references, etc.)

2. **Wire** — The `AppRuntime` creates the agent with:
   - An LLM provider (Anthropic, etc.)
   - Resolved tools (module actions mapped to LLM tool schemas)
   - Memory backends (if configured)
   - Trigger handlers

3. **Run** — The `AgentRuntime` enters the agent loop:
   - Send the system prompt + user input to the LLM
   - LLM responds with text and/or tool calls
   - Execute tool calls (module actions routed through the daemon)
   - Feed results back to the LLM
   - Repeat until the agent stops (no more tool calls, or max turns reached)

## Execution Modes

| Mode | `loop.type` | Behavior |
|------|-------------|----------|
| **Reactive** | `reactive` | Plan, execute, observe, replan (default) |
| **Single-shot** | `single_shot` | LLM acts once and returns |
| **Continuous** | `continuous` | Daemon mode, waits for new triggers |

## CLI Commands

```bash
# Run an app
llmos app run <file.app.yaml> [--input "..."]

# Validate an app
llmos app validate <file.app.yaml>

# List registered apps (daemon mode)
llmos app list

# Show app info
llmos app info <file.app.yaml>
```

## Standalone vs Daemon Mode

### Standalone (default)

When you run `llmos app run`, the app executes locally with a limited set of modules (filesystem + os_exec). No daemon needed.

### Daemon mode

When the LLMOS daemon is running (`llmos-bridge serve`), `llmos app run` automatically connects to it, giving the app access to **all 20 modules** (240+ actions), plus security enforcement, audit logging, and the full event bus.

```bash
# Start daemon (in one terminal)
llmos-bridge serve

# Run app (connects to daemon automatically)
llmos app run hello.app.yaml
```

## Next Steps

- [App Configuration](app-config) — Variables, metadata, interface
- [Agents](agents) — Brain, system prompt, loop configuration
- [Tools](tools) — Available modules and built-in tools
- [Memory](memory) — Persistent memory across sessions
