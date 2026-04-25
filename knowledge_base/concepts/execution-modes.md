---
id: execution-modes
title: "Execution modes (conversation, one_shot, background, pipeline)"
type: concept
keywords: [execution, mode, conversation, one_shot, background, pipeline, entry_agent, max_turns, timeout, greeting, workspace_mode, input, output, triggers, session_mode]
related: [triggers, session-modes, payload-schema, agent-spawn, channels]
source: packages/digitorn/core/app/schema.py
---

# Execution modes -- the 4 ways apps run

## What it is

Every Digitorn app has an **execution mode** that determines how it receives input, how long it runs, and how it interacts with users. The mode is set in the `execution:` block.

## The 4 modes

### 1. conversation -- bidirectional chat

The agent runs as a persistent chat partner. The user sends messages, the agent responds, the conversation continues indefinitely. Sessions persist between activations.

**Use for:** chatbots, coding assistants, tutors, support agents, interactive tools.

```yaml
execution:
  mode: conversation
  entry_agent: main
  max_turns: 200           # Per user message (not per session)
  timeout: 1800            # 30 minutes per turn
  greeting: |
    Ready. What are we working on?
  workspace_mode: required  # User must select a workspace
  workspace: ""             # Or set a fixed path
  project_memory: auto      # Auto-scan for .digitorn.md, CLAUDE.md
```

Key fields for conversation mode:

| Field | Default | Description |
|-------|---------|-------------|
| `greeting` | "" | Welcome message shown at conversation start |
| `workspace_mode` | "auto" | `none`, `required`, `fixed`, `auto` |
| `workspace` | "" | Fixed workspace path (with `fixed` mode) |
| `project_memory` | "auto" | Load project memory file into system prompt |
| `max_turns` | 50 | Max agent loop iterations per user message |
| `timeout` | 300 | Seconds per turn |

### 2. one_shot -- single run, input in, output out

The agent receives one input, processes it, and returns one output. No ongoing conversation. Invoked via `POST /api/apps/{app_id}/run`.

**Use for:** text analysis, code generation, report writing, image processing, data transformation.

```yaml
execution:
  mode: one_shot
  entry_agent: coordinator
  max_turns: 25
  timeout: 600
  input:
    type: text              # text, image, audio, video, file, json, any
    required: true
    description: "Research question"
    accept: []              # MIME types (empty = infer from type)
    max_size: "10MB"
  output:
    type: json              # text, json, markdown, file, image, audio
    description: "Structured report"
    schema:                 # Optional JSON Schema for output
      type: object
      properties:
        summary: { type: string }
        findings: { type: array }
```

Input types and their requirements:

| Type | Requires | Example |
|------|----------|---------|
| `text` | Nothing | Plain text question |
| `image` | Vision-capable model | Image analysis |
| `json` | Nothing | Structured input |
| `file` | Nothing | File processing |
| `any` | Nothing | Accepts anything |

### 3. background -- trigger-driven autonomous agent

The agent runs without direct user interaction, activated by triggers (cron, file watch, HTTP webhook) or channel events. Sessions persist between activations.

**Use for:** monitoring, scheduled reports, webhook processors, email handlers, automation.

```yaml
execution:
  mode: background
  entry_agent: main
  max_turns: 15             # Per activation (not per session)
  timeout: 120              # Per activation
  session_mode: multi       # mono or multi
  max_sessions_per_user: 10
  max_concurrent_activations: 20
  triggers:
    - id: hourly_check
      type: cron
      schedule: "0 * * * *"
      message: "Run the hourly check"
  payload_schema:           # Optional -- typed form for session config
    required: true
    prompt:
      required: true
      label: "What should I monitor?"
```

Key fields for background mode:

| Field | Default | Description |
|-------|---------|-------------|
| `session_mode` | "mono" | `mono` (1 session/user) or `multi` (N sessions/user) |
| `max_sessions_per_user` | 10 | Max sessions per user in multi mode |
| `max_concurrent_activations` | 20 | Throttle parallel LLM calls on broadcast |
| `triggers` | [] | List of trigger definitions |
| `payload_schema` | null | Typed form for session configuration |

### 4. pipeline -- multi-app chain

Chains multiple deployed apps in sequence. Each step's output feeds into the next step's input. All apps must be deployed and in one_shot mode.

**Use for:** multi-step processing, app composition, complex workflows.

```yaml
execution:
  mode: pipeline

pipeline:
  - app: text-extractor
    input: "{{input}}"
  - app: summarizer
    input: "{{steps[0].output}}"
  - app: translator
    input: "{{steps[1].output}}"
    params:
      target_language: "fr"
```

## workspace_mode

Controls how the agent's working directory is handled:

| Value | Behavior |
|-------|----------|
| `none` | No workspace. For chatbots, Q&A agents. |
| `required` | User must select a workspace before chatting. |
| `fixed` | Uses the `workspace` path from YAML. No override allowed. |
| `auto` | Uses YAML workspace if set, allows override per session. |

## entry_agent

Specifies which agent receives the initial message. Defaults to the first agent in the list.

```yaml
agents:
  - id: coordinator       # First agent = default entry
    role: coordinator
  - id: worker
    role: specialist

execution:
  entry_agent: coordinator  # Explicit (same effect here)
```

## Context management

Shared across all modes via `execution.context` or per-agent via `agent.brain.context`:

```yaml
execution:
  context:
    max_tokens: 128000        # 0 = auto-detect from provider
    strategy: summarize       # truncate or summarize
    keep_recent: 10           # Messages to preserve during compaction
    compression_trigger: 0.75 # Token pressure ratio to trigger compaction
    auto_compact: true        # Enable automatic compaction
    summary_max_tokens: 1024  # Max tokens for the summary
```

## Complete examples

### Conversation -- coding assistant

```yaml
app:
  app_id: code-helper
  name: "Code Helper"

modules:
  filesystem: {}
  shell: {}
  memory:
    config:
      working_memory: true

agents:
  - id: main
    role: coordinator
    brain:
      provider: anthropic
      model: claude-sonnet-4-20250514
      backend: anthropic
      config:
        api_key: "claude-code"
    system_prompt: |
      You are a coding assistant.

execution:
  mode: conversation
  workspace_mode: required
  greeting: "Ready. What are we working on?"
  max_turns: 200
  timeout: 1800

capabilities:
  default_policy: block
  grant:
    - module: filesystem
      actions: [read, write, edit, grep, glob]
    - module: shell
      actions: [bash]
    - module: memory
      actions: [set_goal, remember, task_create, task_update]
```

### Background -- scheduled monitor

```yaml
app:
  app_id: price-monitor
  name: "Price Monitor"

modules:
  web:
    config:
      search:
        primary: duckduckgo
  memory:
    config:
      working_memory: true

agents:
  - id: monitor
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
    system_prompt: |
      You are a monitoring agent. Check the data source and report changes.

execution:
  mode: background
  session_mode: multi
  max_turns: 15
  timeout: 120
  triggers:
    - id: check
      type: cron
      schedule: "*/15 * * * *"
      message: "Run the monitoring check"
  payload_schema:
    required: true
    prompt:
      required: true
      label: "What should I monitor?"

channels:
  slack_alerts:
    type: webhook
    config:
      url: "{{secret.SLACK_WEBHOOK}}"

capabilities:
  default_policy: auto
  grant:
    - module: web
      actions: [search, fetch, extract]
    - module: memory
      actions: [set_goal, remember]
```

### One-shot -- document analyzer

```yaml
app:
  app_id: doc-analyzer
  name: "Document Analyzer"

agents:
  - id: analyzer
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
    system_prompt: |
      Analyze the input document and produce a structured report.

execution:
  mode: one_shot
  max_turns: 10
  timeout: 120
  input:
    type: text
    required: true
  output:
    type: json
    schema:
      type: object
      properties:
        summary: { type: string }
        topics: { type: array }
        sentiment: { type: string }
```
