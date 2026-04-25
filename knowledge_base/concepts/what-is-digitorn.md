---
id: what-is-digitorn
title: "What is Digitorn"
type: concept
keywords: [digitorn, framework, agent, daemon, yaml, modules, architecture, overview, fastapi, socketio, conversation, background, one_shot, pipeline, preview, multi-user]
related: [app-structure, app-lifecycle, modules-overview, secrets-credentials, common-errors]
source: docs/
---

# What is Digitorn

## The big picture

Digitorn is a **declarative AI agent framework** in Python. You define apps in YAML -- agents, modules, triggers, channels -- and the daemon compiles and runs them. No boilerplate, no framework code to write. One YAML file can produce a full-featured AI agent with tools, memory, sub-agents, live previews, scheduled tasks, and multi-channel output.

## Architecture

```
                         ┌──────────────────────────────┐
                         │       Digitorn Daemon        │
                         │   (FastAPI + Socket.IO)      │
                         │                              │
  Users ──────────►      │  ┌────────┐  ┌────────────┐ │
  (API / TUI / Flutter)  │  │  Auth  │  │  Sessions  │ │
                         │  └────┬───┘  └─────┬──────┘ │
                         │       │            │        │
                         │  ┌────▼────────────▼──────┐ │
                         │  │     App Manager         │ │
                         │  │  (compile, deploy, run) │ │
                         │  └────┬───────────────┬───┘ │
                         │       │               │     │
                         │  ┌────▼────┐   ┌──────▼───┐ │
                         │  │ Agents  │   │ Modules  │ │
                         │  │ (LLM)   │   │ (tools)  │ │
                         │  └─────────┘   └──────────┘ │
                         └──────────────────────────────┘
```

### Components

- **Daemon** -- a long-running FastAPI + Socket.IO server. Manages apps, sessions, auth, events, secrets, packages. All communication goes through its REST API and SSE/WebSocket streams.

- **Apps** -- defined in YAML (`app.yaml`). Each app declares its agents, modules, capabilities, execution mode, hooks, and channels. The daemon compiles the YAML at deploy time, validates everything, and creates an executable runtime.

- **Modules** -- Python plugins that provide **actions** (tools) to agents. Examples: `filesystem` (read/write/edit/glob/grep), `shell` (bash), `web` (search/fetch), `memory` (goal/remember/todo), `database` (SQL), `workspace` (virtual files for live previews). Modules are the bridge between the LLM and the real world.

- **Agents** -- LLM-powered entities that receive user messages, reason, and call module actions. Each agent has a **brain** (provider + model config), a system prompt, and a role. Apps can have multiple agents: a coordinator that spawns specialist sub-agents in parallel.

- **Sessions** -- per-user conversation state. Each session tracks messages, memory (goal, facts, todos), context window, and workspace files. Sessions are isolated between users.

## Key concepts

### Modules are tools

Every module exposes a set of **actions** the agent can call as tools. The framework handles:
- Tool name resolution (short names like `Read`, `Write`, `Bash` mapped to FQNs like `filesystem.read`)
- Parameter validation (Pydantic models with auto-coercion)
- Hidden parameters (excluded from the LLM schema but usable internally)
- Tool prompts (detailed instructions injected into the system prompt)

### Capabilities control access

The `capabilities:` block in app.yaml is a security layer:

```yaml
capabilities:
  default_policy: block          # block everything by default
  max_risk_level: medium
  grant:
    - module: filesystem
      actions: [read, write, edit, glob, grep]
    - module: shell
      actions: [bash]
  deny:
    - module: database
      actions: [execute_query]
      reason: "Read-only mode"
  hidden_modules: [preview]      # loaded but invisible to the agent
```

Policies: `grant` (auto-approve), `approve` (ask user first), `deny` (block), `block` (default for unlisted).

### Hooks automate runtime events

Hooks fire during the agent loop on events like `turn_end`, `tool_start`, `tool_end`:

```yaml
execution:
  hooks:
    - id: auto_compact
      on: turn_end
      condition:
        type: context_pressure
        threshold: 0.75
      action:
        type: compact_context
        strategy: summarize
        keep_last: 10
      cooldown: 30
```

15 hook events, 10 condition types, 11 action types. Hooks can gate tool execution, transform parameters/results, run shell commands, and chain multiple actions.

## Execution modes

### `conversation` -- interactive chat

The agent loops: receive message, think, call tools, respond. Session persists across turns. Used for coding assistants, Q&A bots, interactive analysis.

```yaml
execution:
  mode: conversation
  max_turns: 200
  timeout: 1800
  greeting: "Ready. What are we working on?"
```

### `one_shot` -- run once

The agent receives one input, processes it, returns one output. No session persistence. Used for transformations, analysis, code review.

```yaml
execution:
  mode: one_shot
  max_turns: 50
  timeout: 300
  input:
    type: text
    required: true
  output:
    type: json
    schema:
      type: object
      properties:
        summary: { type: string }
        score: { type: integer }
```

### `background` -- trigger-driven

The agent runs without user interaction, activated by triggers (cron, file watch, HTTP webhook). Each trigger sends a message to the agent. Used for monitoring, automation, scheduled reports.

```yaml
execution:
  mode: background
  session_mode: multi           # mono (1 session/user) or multi (N sessions/user)
  max_sessions_per_user: 10
  triggers:
    - id: every_morning
      type: cron
      schedule: "0 8 * * *"
      message: "Good morning. Check for new tasks."
    - id: new_file
      type: watch
      paths: ["./inbox/*.csv"]
      message: "New file: {{event.path}}"
    - id: webhook
      type: http
      path: /incoming
      port: 9100
      method: POST
      message: "Webhook received: {{event.body}}"
      routing: session
      routing_key: "{{event.header.X-Session-Id}}"
```

### `pipeline` -- chain apps

Run multiple deployed apps in sequence, passing output from one to the next. Used for multi-step workflows.

```yaml
execution:
  mode: pipeline
pipeline:
  - app: code-reviewer
    input: "{{input}}"
  - app: report-generator
    input: "{{steps[0].output}}"
```

## Live previews

Apps can include a **workspace** (virtual file system) and a **preview** (live rendering). The agent writes files with `WsWrite`/`WsEdit`, and the client renders them in real time.

```yaml
workspace:
  render_mode: react            # react, html, markdown, slides, code, latex, builder
  entry_file: src/App.tsx
  title: "My App"

modules:
  workspace:
    config:
      render_mode: react
      entry_file: src/App.tsx
      lint: true
  preview: {}
```

Render modes: `react` (React/Vite), `html` (static HTML), `markdown`, `slides` (presentations), `code` (code editor), `latex` (PDF), `builder` (visual builder).

Two preview strategies:
1. **Dev server** (`preview.enabled: true`) -- daemon spawns Vite, proxies HTTP + WebSocket. Supports HMR.
2. **Static bundle** -- daemon serves pre-built `web/dist/` directly. Zero overhead.

## Multi-user

- Each user authenticates via JWT (or Claude Code OAuth token)
- Users have isolated sessions, secrets, and credentials
- Apps can be system-wide (visible to all) or user-scoped (personal)
- Background apps support per-user sessions with custom payload schemas
- Channels can auto-resolve user contact info (email, phone) from a database

## Minimal working app

```yaml
app:
  app_id: hello
  name: "Hello Agent"

agents:
  - id: main
    brain:
      provider: anthropic
      model: claude-sonnet-4-20250514
      backend: anthropic
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"

execution:
  mode: conversation
  greeting: "Hello! How can I help?"
```

Deploy with `POST /api/apps/deploy {yaml: "..."}` or `digitorn run app.yaml`.

## See also

- app-structure -- how to organize an app project
- app-lifecycle -- the complete lifecycle from idea to production
- modules-overview -- what modules are and how they work
- secrets-credentials -- managing API keys and credentials
- common-errors -- troubleshooting compilation and runtime errors
