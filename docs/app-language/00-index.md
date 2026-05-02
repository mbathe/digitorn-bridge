---
id: index
---

# Digitorn App Language Reference

Digitorn apps are declared in a single YAML file. The compiler parses
that YAML into the `AppDefinition` Pydantic model
(`packages/digitorn/core/app/schema.py:2619`) and the daemon runs it.

There is **one canonical schema (v2)** with **8 top-level blocks**.
Every field has exactly one home; legacy flat YAMLs (`execution:`,
`modules:` at the top level, ...) are still accepted by an alias pass
that reshapes them to canonical before validation.

The optional `schema_version: 2` declaration at the top of the file
future-proofs against breaking changes.

## The 8 blocks

| Block | Required | What it holds | Doc |
|-------|----------|---------------|-----|
| `app:` | **Yes** | Identity — `app_id`, `name`, `version`, `icon`, `color`, `tags`, `quick_prompts`. | [App Configuration](02-app-config.md) |
| `runtime:` | No (defaults) | Lifecycle — `mode`, `entry_agent`, `max_turns`, `timeout`, `triggers`, `hooks`, `middleware`, `pipeline`, `context`, `workdir`, `default_channel`. | [App Configuration](02-app-config.md), [Triggers](09-triggers.md), [Middleware](17-middleware.md), [Tool Hooks](31-tool-hooks.md), [Context Management](06-context-management.md) |
| `agents:` | At least 1 in practice | List of agents. Each has `id`, `role`, `brain`, `system_prompt`, `modules`, `pool`, `delegate_to`. | [Agents](03-agents.md), [Multi-Agent](12-multi-agent.md) |
| `tools:` | No | What the agent can call: `modules` (dict), `capabilities` (grant / deny), `channels` (dict). | [Tools](04-tools.md), [Built-in Tools](04b-builtin-tools.md), [MCP Servers](04d-mcp.md), [Channels](40-channels.md), [Security](11-security.md) |
| `security:` | No | Runtime boundaries: `behavior`, `sandbox`, `credentials_schema`. | [Behavior Engine](43-behavior.md), [OS Sandbox](35-sandbox.md), [credentials.md](../credentials.md) |
| `ui:` | No | Pure display, never read by the daemon: `theme`, `features`, `widgets`, `workspace` (renderer), `preview`, `slash_commands`, `quick_prompts`, `greeting`. | [Client Manifest](44-client-manifest.md), [Widgets](42-widgets.md), [Workspace & Preview](41-preview.md) |
| `dev:` | No | Developer affordances: `skills`, `variables`, `include` (fragmentation). | [Skills System](21-skills.md), [Bundle namespaces](38-bundle-namespaces.md) |
| `flow:` | No | Optional declarative orchestration graph for multi-agent apps. Top-level since v2 because flow is a paradigm shift (explicit scenography vs implicit `Agent()` coordination). | [Flows](07-flows.md) |

The `ui.workspace` block (renderer) is a different concept from
`runtime.workdir` (filesystem path). The schema renames the legacy
`execution.workspace` to `runtime.workdir` to remove the ambiguity
(`schema.py:2394`).

## Quick example

```yaml
app:
  app_id: my-assistant
  name: My Assistant

runtime:
  mode: conversation

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
      You are a helpful coding assistant.
      Workspace: {{workspace}}

tools:
  modules:
    filesystem:
      constraints:
        allowed_actions: [read, glob, grep]
  capabilities:
    default_policy: auto

ui:
  greeting: "Hello! How can I help?"

dev:
  variables:
    workspace: "{{env.PWD}}"
```

Deploy and chat with it:

```bash
digitorn start                                 # daemon if not running
digitorn app run my-assistant.yaml             # deploy + arm triggers
digitorn dev chat my-assistant -m "Hi there!"  # talk to it
```

## Migration from the legacy flat shape

If your YAML has `execution:`, `modules:`, `channels:`, `behavior:`, ...
at the top level, the compiler still accepts it via the alias pass
(`packages/digitorn/core/app/schema_aliases.py`). The bidirectional
mirror means both shapes work at read time.

To rewrite the file in-place to the canonical 8-block form:

```bash
digitorn yaml migrate-v2 path/to/app.yaml
```

Field renames the migrator applies (no compat retention). Each row
shows the legacy v1 path on the left and the v2 canonical path on
the right:

| Legacy v1 | Canonical v2 |
|--------|-----------|
| `execution.workspace` | `runtime.workdir` |
| `execution.workspace_mode` | `runtime.workdir_mode` |
| `execution.greeting` | `ui.greeting` |
| `execution.sandbox` | `security.sandbox` |
| `execution.credentials_schema` | `security.credentials_schema` |
| `dependencies.variables` | `dev.variables` |
| `dependencies.channels` | `tools.channels` |
| `dependencies.credentials` | `security.credentials_schema` |
| `dependencies.payload` | `runtime.payload_schema` |

Top-level lifts (legacy → canonical home, fields keep their name):

| Legacy top-level (v1) | Canonical (v2) |
|------------------|-----------|
| `modules:` | `tools.modules` |
| `capabilities:` | `tools.capabilities` |
| `channels:` | `tools.channels` |
| `behavior:` | `security.behavior` |
| `widgets:` | `ui.widgets` |
| `workspace:` (block at root) | `ui.workspace` (renderer) |
| `preview:` | `ui.preview` |
| `theme:` | `ui.theme` |
| `features:` | `ui.features` |
| `slash_commands:` | `ui.slash_commands` |
| `skills:` | `dev.skills` |
| `variables:` | `dev.variables` |
| `include:` | `dev.include` |
| `middleware:` | `runtime.middleware` |
| `pipeline:` | `runtime.pipeline` |
| `flow:` (in v1 was top-level too, but is now strictly canonical) | `flow:` (top-level, NOT under runtime) |

Everything that was under `execution:` (`mode`, `triggers`, `hooks`,
`max_turns`, `timeout`, `session_mode`, `direct_modules`,
`tool_injection`, `default_channel`, `context`, `payload_schema`,
`watchers`, `scheduler`, ...) lifts to `runtime:` with the same name.
`security.sandbox` → `security.sandbox`,
`security.credentials_schema` → `security.credentials_schema`,
`ui.greeting` → `ui.greeting`.

## Documentation by topic

### Getting started

- [Getting Started](01-getting-started.md) — install, first app, run loop
- [App Configuration](02-app-config.md) — exhaustive reference for the 8 blocks
- [Examples](15-examples.md) — complete real-world apps

### Agents and tools

- [Agents](03-agents.md) — agent definition, brain, providers, fallback
- [Multi-Agent](12-multi-agent.md) — coordinator + specialists, `agent_spawn`, isolation
- [Tools](04-tools.md) — adaptive tool injection, discovery, semantic search
- [Built-in Tools](04b-builtin-tools.md) — delegation, memory, todo, messaging
- [Execution Primitives](04c-primitives.md) — parallel execution, watchers, scheduler
- [MCP Servers](04d-mcp.md) — connect external MCP servers, sandbox, OAuth2
- [Web Module](19-web.md) — search + fetch + parse
- [LSP Diagnostics](27-lsp.md) — real-time code diagnostics

### Memory and context

- [Cognitive Memory](05-memory.md) — working memory, tasks, notes, facts
- [Context Management](06-context-management.md) — compaction, summary brain, hooks
- [Advanced RAG](37-rag.md) — hybrid retrieval, citations, semantic cache, Text2SQL

### Runtime control

- [Triggers](09-triggers.md) — cron, watch, http (background mode)
- [Flows](07-flows.md) — declarative orchestration graph
- [Middleware Pipeline](17-middleware.md) — secret masking, content filter, RAG inject
- [Tool Hooks](31-tool-hooks.md) — pre/post hooks around tool calls
- [Skills System](21-skills.md) — `/commit`, `/review`, custom commands
- [Channels (Bidirectional I/O)](40-channels.md) — webhooks, cron, email, RSS
- [Background Sessions](38-background-sessions.md) — mono / multi session modes
- [Macros](08-macros.md) — reusable YAML fragments
- [Composition](22-composition.md) — referencing other apps
- [Rules](33-rules.md) — modular project instructions

### Security

- [Capabilities](11-security.md) — `default_policy`, grant / deny, approve gates
- [Behavior Engine](43-behavior.md) — declarative runtime rules + classifier
- [OS Sandbox](35-sandbox.md) — Landlock, seccomp, Seatbelt, Job Objects
- [Auth](22-auth.md) — JWT, per-user installs

### UI and client

- [Client Manifest](44-client-manifest.md) — `features`, `theme`, `slash_commands`
- [Widgets](42-widgets.md) — declarative UI primitives
- [Workspace & Preview](41-preview.md) — virtual filesystem streamed to the client
- [Bundle namespaces](38-bundle-namespaces.md) — `{{prompt.X}}`, `{{include:}}`, hot reload

### Operating and deploying

- [Daemon Configuration](23-configuration.md) — server, KV, database, CORS
- [Observability & Monitoring](24-observability.md) — metrics, health, tracing
- [Production Deployment](36-production.md) — TLS, rate limiting, hardening
- [Multi-Tenant Installs](45-multi-tenant.md) — per-user vs system-wide
- [Bundle namespaces](38-bundle-namespaces.md) — fragmentation, i18n, hot reload
- [Dev CLI](46-dev-cli.md) — test against the real daemon
- [API Integration](14-api-integration.md) — REST + Socket.IO contracts
- [Expressions](10-expressions.md) — template language
- [App-as-MCP-Server](16-app-as-mcp-server.md) — *(planned)* expose deployed apps

## Modules

The daemon ships **22 modules** (under `packages/digitorn/modules/`):

`agent_spawn`, `behavior`, `channels`, `context_builder`, `cron_native`,
`database`, `dev_tools`, `filesystem`, `http`, `index`, `llm_provider`,
`lsp`, `mcp`, `memory`, `preview`, `queue`, `rag`, `shell`, `vector`,
`web`, `widget`, `workspace`.

`context_builder` and `llm_provider` are auto-loaded; you never declare
them under `tools.modules`. Per-module reference docs live under
[modules/reference/](../modules/index.md).

## Architecture

```
                     ┌─────────────────┐
   app.yaml  ───────▶│ AppYAMLCompiler │   schema_aliases ▶ Pydantic validate
                     │ compiler.py:1241│
                     └────────┬────────┘
                              │ resolve variables, secrets, capabilities
                              ▼
                       CompiledApp                ──── compiler.py:1094
                              │
                              ▼
                        bootstrap()               ── instantiate modules
                              │
                              ▼
                        RuntimeApp                ──── runtime/app.py:20
                              │
              ┌───────────────┼─────────────────┐
              ▼               ▼                 ▼
       AgentContext      HookRunner       ContextBuilder
   runtime/types.py:31  hooks.py:2986      (auto-loaded module)
              │
              ▼
         agent_turn()
```

The compiler walks the YAML once, validates against the eight Pydantic
blocks, then bootstraps each declared module. Once running, every tool
call goes through the `AgentContext`, hooks fire around it via
`HookRunner`, and `ContextBuilder` decides what tool schemas reach the
LLM (direct vs discovery vs compact).

### Tool delivery — direct, compact, or discovery

The `ContextBuilder` module exposes meta-tools the agent can use to
discover capabilities lazily:

- **direct** — full tool schemas injected up front. Best for small
  apps (< ~30 tools).
- **compact_direct** — tool names + 1-line descriptions, full schema
  on demand via `get_tool`.
- **discovery** — only `list_categories`, `browse_category`,
  `search_tools`, `get_tool`, `execute_tool` injected. Agent walks
  the tree as needed. Scales to hundreds of tools.

The mode is auto-detected by the compiler based on tool count, or
forced via `runtime.tool_injection`.

### LLM compatibility

Three backends are supported
(`AgentBrain.backend: Literal["openai_compat", "anthropic", "github_copilot"]`
in `schema.py`, default `openai_compat`):

- `openai_compat` — any OpenAI-compatible `/v1` endpoint
  (OpenAI, DeepSeek, Groq, Mistral, Together, Ollama, vLLM, LM Studio,
  OpenRouter, Cerebras, Perplexity, Fireworks, xAI, Gemini, ...).
- `anthropic` — Anthropic SDK (also accepts the `claude-code` API-key
  alias for Claude Code OAuth tokens, see
  [`packages/digitorn/modules/llm_provider/providers/anthropic.py`](../modules/reference/llm_provider.md)).
- `github_copilot` — uses your GitHub Copilot subscription.

Models that support **native tool calling** (OpenAI, Anthropic,
DeepSeek, Groq, Mistral, Together) get tools via the API
`tools=` parameter. Models that don't (Ollama, LM Studio, vLLM, small
local models) get tool schemas injected into the system prompt; tool
calls are parsed from the text output via a multi-format recovery
parser.

## CLI

The `digitorn` command is exposed by the `digitorn` PyPI package
(entry point `digitorn = "digitorn.core.server:main"`). The
sub-commands below are all registered in `core/server.py:1708-1722`
via Typer.

```bash
# First-run wizard + environment doctor
digitorn init
digitorn doctor

# Apps (validate, deploy, run, schema, list, undeploy, delete)
digitorn app validate <app.yaml>
digitorn app deploy <app.yaml>
digitorn app run <app.yaml>             # equivalent to deploy --force, shows triggers
digitorn app schema <module_id>
digitorn app list
digitorn app undeploy <app_id>
digitorn app delete <app_id>

# Per-app encrypted secrets
digitorn secret set <app_id> <key> [value]
digitorn secret get <app_id> <key>
digitorn secret list <app_id>
digitorn secret delete <app_id> <key>

# YAML migration
digitorn yaml migrate-v2 <app.yaml>
digitorn yaml migrate-credentials <app.yaml>

# MCP servers
digitorn mcp install <server>
digitorn mcp list
digitorn mcp uninstall <server_id>

# Middleware
digitorn middleware list
digitorn middleware install <path>
digitorn middleware uninstall <middleware_id>

# Dev loop (test apps against the live daemon)
digitorn dev deploy <app.yaml>
digitorn dev chat <app_id> [-m "message"]
digitorn dev status <app_id>
digitorn dev history <app_id>

# Daemon control (top-level commands defined in server.py)
digitorn start [--host 127.0.0.1] [--port 8000] [--workers N] [--config config.yaml] [--app app.yaml]
digitorn stop [--host 127.0.0.1] [--port 8000]
digitorn status [--host 127.0.0.1] [--port 8000]
digitorn version

# Module catalog + credential vault + KB + hub
digitorn modules ...
digitorn credentials ...
digitorn requires ...
digitorn package ...
digitorn hub ...
digitorn install ...
digitorn db ...
```

`digitorn` with no arguments prints Typer help. For systemd / launchd
/ Windows service installation, see
[Production Deployment](36-production.md).
