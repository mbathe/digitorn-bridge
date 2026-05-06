---
id: getting-started
---

# Getting Started

This guide walks you through creating, validating, and running your
first Digitorn application.

## Prerequisites

- **Python 3.12+** (`pyproject.toml: python = "^3.12"`)
- Digitorn installed (`pip install digitorn`, or from source)
- An LLM API key (DeepSeek, OpenAI, Anthropic, Groq, ...) **or** a
  local model server (Ollama, LM Studio, vLLM)

## Your first app

Save the following as `hello.yaml`. This example uses a local Ollama
model so no API key is required. To use a cloud provider instead,
swap the `brain` block — see [Using different providers](#using-different-providers).

```yaml
app:
  app_id: hello
  name: "Hello App"
  description: "My first Digitorn app"

runtime:
  mode: conversation

agents:
  - id: assistant
    role: assistant
    brain:
      provider: ollama
      model: qwen25-7b-gpu:latest
      backend: openai_compat
      config:
        base_url: http://localhost:11434/v1
        api_key: ollama
    system_prompt: |
      You are a friendly assistant. Answer questions concisely.

tools:
  modules:
    memory:
      config:
        auto_remember: false
  capabilities:
    default_policy: auto

ui:
  greeting: "Hello! I'm your assistant. Ask me anything."
```

To use a cloud provider, replace the `brain` block with one of the
configurations from [Using different providers](#using-different-providers)
and export the matching API key.

## Running the app

The CLI surface for apps lives under `digitorn app *` (registered in
`packages/digitorn/core/cli/app.py`).

### Validate without running

```bash
digitorn app validate hello.yaml
```

Compiles the YAML through `AppYAMLCompiler`
(`packages/digitorn/core/app/compiler.py`) and reports any error
before bootstrap. A green checkmark means the app definition is
structurally correct.

### Deploy + run on a daemon

```bash
# Start the daemon if not already running
digitorn start

# Deploy the app and start its background triggers (if any)
digitorn app run hello.yaml         # equivalent to: app deploy --force

# Confirm it's listed
digitorn app list

# Talk to it interactively from the dev CLI
digitorn dev chat hello             # interactive chat loop
digitorn dev chat hello -m "Say hello in three languages"  # one-shot
```

`digitorn app run` deploys the YAML to the daemon and surfaces trigger
status for background-mode apps. It does NOT open an interactive
session - that's `digitorn dev chat <app_id>`. The dev chat command
auto-approves any pending capability prompts and is the simplest way
to test a deployed app from the terminal.

## What validation checks

The compiler runs every check at compile time so structural problems
never reach runtime:

1. **YAML syntax** — valid mapping at the root.
2. **Schema validation** (Pydantic with `extra: forbid` on every
   block) — types, required fields, literal sets, value ranges.
3. **Variable resolution** — every `{{...}}` reference must resolve.
   Missing `{{env.X}}` raises a compile error (use `??` for
   optional values).
4. **Provider hint** — the `brain.provider` value must be in the
   known set (validated by `AgentBrain` in `schema.py`); typos
   get a "Did you mean..." suggestion.
5. **Module existence** — every key under `tools.modules` must match
   a registered module.
6. **Action existence** — every `setup[].action` must exist on its
   module; `params` are validated against the action's
   `params_model`.
7. **Capability resolution** — `tools.capabilities.grant`,
   `approve`, `deny`, `hidden_actions` are compiled into a
   `SecurityProfile`.
8. **Per-agent module restriction** — every entry under
   `agents[].modules` is shape-checked at validation time.

A green `digitorn app validate` means the app definition is
structurally correct. Runtime can still fail (an external API is
down, a file isn't where you expect), but the YAML itself is sound.

## How it works

```
   hello.yaml ──▶ AppYAMLCompiler ──▶ CompiledApp
                                          │
                                          ▼
                                     bootstrap()    (instantiate modules,
                                          │         push configs, run setup,
                                          ▼         build tool index)
                                      RuntimeApp
                                          │
                                          ▼
                                     agent_turn()   (per-turn loop)
```

Every turn:

1. The system prompt + the user input are sent to the LLM.
2. The LLM responds with text and / or tool calls.
3. Tool calls are routed through the context builder
   (`context_builder.execute_tool()`).
4. Results stream back to the LLM via the next iteration of the loop.
5. The loop ends when the LLM emits no more tool calls or
   `runtime.max_turns` is reached.

## Execution modes

`runtime.mode` (`RuntimeBlock` in `schema.py`):

| Mode | Behavior |
|------|----------|
| `one_shot` | Process a single input via `runtime.input` / `runtime.output` and return. |
| `conversation` (default) | Interactive multi-turn chat loop. |
| `background` | Daemon-driven; triggered by `runtime.triggers` (cron, file watcher, http webhook, RSS, ...). |
| `pipeline` | Multi-app sequencing via `runtime.pipeline[]`. |

See [Triggers](09-triggers.md) for `background` mode, and the
`runtime.input` / `runtime.output` contracts for `one_shot` in
[App Configuration → runtime](02-app-config.md#runtime--lifecycle-and-execution-policy).

## Using different providers

### Cloud providers (API key)

```yaml
# DeepSeek
brain:
  provider: deepseek
  model: deepseek-chat
  backend: openai_compat
  config:
    api_key: "{{env.DEEPSEEK_API_KEY}}"

# OpenAI
brain:
  provider: openai
  model: gpt-4o
  backend: openai_compat
  config:
    api_key: "{{env.OPENAI_API_KEY}}"

# Anthropic (native backend)
brain:
  provider: anthropic
  model: claude-sonnet-4-5
  backend: anthropic
  config:
    api_key: "{{env.ANTHROPIC_API_KEY}}"

# Anthropic via Claude Code OAuth
brain:
  provider: anthropic
  model: claude-sonnet-4-5
  backend: anthropic
  config:
    api_key: "claude-code"          # alias - reads ~/.claude/.credentials.json

# Groq (fast inference)
brain:
  provider: groq
  model: llama-3.3-70b-versatile
  backend: openai_compat
  config:
    api_key: "{{env.GROQ_API_KEY}}"
    base_url: "https://api.groq.com/openai/v1"
```

The full list of validated provider hints and the model choices
for each are documented in
[Agents → Validated provider hints](03-agents.md#validated-provider-hints).

### Local providers (no API key)

```yaml
brain:
  provider: ollama
  model: qwen2.5:14b-instruct-q4_K_M
  backend: openai_compat
  config:
    base_url: "http://localhost:11434/v1"
  context:
    max_tokens: 8000
    strategy: truncate
    keep_recent: 6
```

Defaults for local providers (Ollama, LM Studio, vLLM) :

- **Text-based tool calling** is auto-selected — tool schemas land in
  the system prompt and tool calls are parsed from the model's text
  output by the recovery parser
  ([Agents → Tool-call recovery](03-agents.md#tool-call-recovery)).
- Some local models (e.g. `qwen2.5-coder`, `llama-3.3-70b` on certain
  Ollama builds) support native tool calling. Override with
  `native_tool_use: true`:

  ```yaml
  brain:
    provider: ollama
    model: qwen2.5-coder:7b
    native_tool_use: true
    config:
      base_url: "http://localhost:11434/v1"
  ```

## Deploy to a running daemon

Validation runs in-process. To run the app **inside the long-running
daemon** (background mode, web access, Socket.IO streaming) deploy it:

```bash
# Start the daemon if not already running
digitorn start

# Deploy the app and confirm it's listed
digitorn app deploy hello.yaml
digitorn app list

# Talk to it from the dev CLI (auto-approves any pending capability prompts)
digitorn dev chat hello -m "Say hello in three languages"

# Tear it down
digitorn app undeploy hello
```

`digitorn start`, `digitorn stop`, `digitorn status`, and
`digitorn version` are top-level commands defined in
`packages/digitorn/core/server.py`. The dev workflow (deploy / chat
/ status / history with auto-approval) is covered in
[Dev CLI](46-dev-cli.md).

## Useful CLI commands

```bash
# Apps
digitorn app validate <app.yaml>           # compile-check, no deploy
digitorn app run <app.yaml>                # deploy + start triggers (no message arg)
digitorn app deploy <app.yaml>             # alias for run without trigger summary
digitorn app schema <module_id>            # dump a module's action schema
digitorn app list                          # list deployed apps
digitorn app undeploy <app_id>             # stop without removing the bundle
digitorn app delete <app_id>               # remove the deployed bundle entirely

# Send messages to a deployed app (auto-approves pending prompts)
digitorn dev chat <app_id>                 # interactive
digitorn dev chat <app_id> -m "message"    # one-shot

# Per-app secrets (encrypted vault)
digitorn secret set <app_id> <key> [value]
digitorn secret get <app_id> <key>
digitorn secret list <app_id>
digitorn secret delete <app_id> <key>

# Migrate a legacy YAML to the canonical 8-block form
digitorn yaml migrate-v2 <app.yaml>

# Daemon control (defined in core/server.py)
digitorn start [--host 127.0.0.1] [--port 8000] [--workers N] [--config config.yaml] [--app app.yaml]
digitorn stop
digitorn status
digitorn version
```

The full surface (MCP servers, middleware, modules catalog,
credentials vault, hub, install, db) is listed in the
[index](00-index.md#cli).

## Next steps

- [App Configuration](02-app-config.md) — full reference for the 8
  blocks
- [Agents](03-agents.md) — brain configuration, providers, fallback,
  multi-agent
- [Tools](04-tools.md) — tool discovery, direct vs compact vs
  discovery delivery
- [Context Management](06-context-management.md) — compaction,
  summary brain, token budget
- [Examples](15-examples.md) — complete real-world apps
