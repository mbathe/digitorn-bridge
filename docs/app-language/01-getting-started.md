---
id: getting-started
---

# Getting Started

This guide walks you through creating and running your first Digitorn application.

## Prerequisites

- Python 3.11+
- Digitorn installed (`pip install digitorn` or from source)
- An LLM API key (DeepSeek, OpenAI, Groq, etc.) — or a local Ollama instance

## Your First App

Create a file called `hello.yaml`:

```yaml
app:
  app_id: hello
  name: "Hello App"
  description: "My first Digitorn app"

modules:
  filesystem:
    constraints:
      allowed_actions: [read, glob, grep]

agents:
  - id: assistant
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
    system_prompt: |
      You are a friendly assistant. Answer questions concisely.
      You have access to tools — use them when relevant.

execution:
  mode: conversation
  greeting: "Hello! I'm your assistant. Ask me anything."

capabilities:
  default_policy: auto
```
## Running the App

### Interactive mode (conversation)

```bash
digitorn run hello.yaml
```

This starts an interactive session. Type messages, get responses. Press `Ctrl+C` to exit.

### One-shot mode (single input)

```bash
digitorn run hello.yaml "Say hello in 3 languages"
```

The agent processes the input once and exits.

### Validate without running

```bash
digitorn app validate hello.yaml
```

Checks YAML syntax, validates all modules, actions, params, and constraints against the loaded module registry.

### What Validation Guarantees

The `AppYAMLCompiler` performs multi-layer validation at compile time — before any code runs:

1. **YAML syntax** — Valid YAML, correct structure
2. **Schema validation** (Pydantic) — Every field is type-checked against the schema models
3. **Variable resolution** — All `{{variable_name}}` references must resolve
4. **Module existence** — Every module ID in `modules:` must be registered
5. **Action existence** — All setup step actions must exist on their module
6. **Params validation** — Action params validated against each action's Pydantic `params_model`
7. **Constraint validation** — Module constraints validated against `ConstraintSpec` declarations
8. **Security profile** — `capabilities:` block compiled into a `SecurityProfile`

**If validation passes, the app structure is guaranteed correct.** Runtime errors can still occur (e.g., a file doesn't exist, an API is down), but the app definition itself will not have structural issues.

## How It Works

1. **Compile** — The `AppYAMLCompiler` parses the YAML, resolves `{{variables}}`, and validates all references against the module registry

2. **Bootstrap** — The runtime:
   - Instantiates and starts modules
   - Pushes configs, runs setup steps
   - Builds the tool index via `context_builder`
   - Creates an `AgentContext` per agent (provider, system prompt, tools)
   - Wires the hook runner for auto-compaction

3. **Run** — The `agent_turn()` loop:
   - Sends the system prompt + user input to the LLM
   - LLM responds with text and/or tool calls
   - Tool calls routed through `context_builder.execute_tool()`
   - Results fed back to the LLM
   - Repeats until the agent stops (no more tool calls, or max turns reached)

## Execution Modes

| Mode | `execution.mode` | Behavior |
|------|-------------------|----------|
| **One-shot** | `one_shot` | Process a single input and return |
| **Conversation** | `conversation` | Interactive chat loop |
| **Background** | `background` | Daemon mode, triggered by events |

## Using Different Providers

### Cloud providers (with API keys)

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

# Groq (fast inference)
brain:
  provider: groq
  model: llama-3.3-70b-versatile
  backend: openai_compat
  config:
    api_key: "{{env.GROQ_API_KEY}}"
    base_url: "https://api.groq.com/openai/v1"
```
### Local providers (no API key)

```yaml
# Ollama (auto-detected: text-based tool calling)
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
For local models, Digitorn automatically:
- Detects that the provider doesn't support native tool calling
- Injects tool schemas into the system prompt
- Parses tool calls from the LLM's text output

Some local models (e.g., `qwen2.5-coder`) support native tool calling. Override the auto-detection with `native_tool_use: true`:

```yaml
brain:
  provider: ollama
  model: qwen2.5-coder:7b
  native_tool_use: true
```
## CLI Commands

```bash
# Run an app
digitorn run <app.yaml> [message]
digitorn run <app.yaml> --input file.txt
digitorn run <app.yaml> --image screenshot.png

# Validate an app
digitorn app validate <app.yaml>

# Show module schema
digitorn app schema <module_id>

# Deploy to daemon
digitorn app deploy <app.yaml>
digitorn app list
digitorn app undeploy <app_id>

# Daemon management
digitorn service start
digitorn service stop
digitorn service status
```

## Next Steps

- [App Configuration](02-app-config.md) — Variables, modules, metadata
- [Agents](03-agents.md) — Brain, system prompt, providers
- [Tools](04-tools.md) — Tool discovery and module tools
- [Context Management](06-context-management.md) — Compaction and token budget
