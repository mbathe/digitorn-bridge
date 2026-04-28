---

id: agents
title: Agents
sidebar_position: 3
format: md
---


# Agents

An agent is an LLM with a system prompt, tools, and a loop configuration. Every app has at least one agent.

## Single Agent

Use the `agent:` block for single-agent apps:

```yaml
agent:
  brain:
    provider: anthropic
    model: claude-sonnet-4-20250514
    temperature: 0.2
    max_tokens: 8192
  system_prompt: |
    You are a helpful coding assistant.
    Workspace: {{workspace}}
  tools:
    - module: filesystem
      action: read_file
    - module: os_exec
      action: run_command
  loop:
    type: reactive
    max_turns: 30
```

## Brain Configuration

The `brain:` block configures the LLM provider and model.

```yaml
agent:
  brain:
    provider: anthropic              # LLM provider
    model: claude-sonnet-4-20250514  # Model ID
    temperature: 0.2                 # Sampling temperature (0-2, default: 0)
    max_tokens: 8192                 # Max output tokens (1-200000, default: 8192)
    top_p: 1.0                       # Top-p sampling (0-1, default: 1.0)
    timeout: 120.0                   # LLM call timeout in seconds (default: 120)
    config: {}                       # Provider-specific extra config

    # Fallback models (tried in order if primary fails)
    fallback:
      - provider: anthropic
        model: claude-haiku-4-5-20251001
        config: {}
```

### Brain Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `provider` | string | `"anthropic"` | LLM provider name |
| `model` | string | `"claude-sonnet-4-6"` | Model identifier |
| `temperature` | float | `0` | Sampling temperature (0 = deterministic) |
| `max_tokens` | int | `8192` | Max output tokens per response |
| `top_p` | float | `1.0` | Nucleus sampling parameter |
| `timeout` | float | `120.0` | Timeout per LLM call (seconds, 0 = no timeout) |
| `config` | dict | `{}` | Provider-specific configuration |
| `fallback` | list | `[]` | Fallback models tried in order |

### Supported Providers

| Provider | API | Models (examples) |
|----------|-----|-------------------|
| `anthropic` | Anthropic SDK | `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001` |
| `openai` | OpenAI API | `gpt-4o`, `gpt-4o-mini`, `o3` |
| `google` | Google AI Studio (OpenAI-compatible) | `gemini-2.0-flash`, `gemini-1.5-pro` |
| `ollama` | Ollama local (OpenAI-compatible) | `llama3.1:8b`, `qwen2.5-coder:7b`, `mistral` |
| Any other | OpenAI-compatible API | Any model - just provide `base_url` and `api_key` in `config` |

All providers except `anthropic` use the OpenAI-compatible API format (`/v1/chat/completions`). This means **any OpenAI-compatible service works without code changes** - just set `base_url` and `api_key` in the `config:` block.

#### Provider-Specific Defaults

| Provider | Default `base_url` | Default `api_key` |
|----------|-------------------|-------------------|
| `ollama` | `http://localhost:11434/v1` | `ollama` |
| `google` | `https://generativelanguage.googleapis.com/v1beta/openai/` | (none) |
| Others | (none - must be set in `config`) | (none) |

#### Examples

```yaml
# Anthropic (native SDK)
brain:
  provider: anthropic
  model: claude-sonnet-4-20250514
  config:
    api_key: "{{secret.ANTHROPIC_API_KEY}}"

# Google AI Studio (free tier)
brain:
  provider: google
  model: gemini-2.0-flash
  config:
    api_key: "{{secret.GOOGLE_API_KEY}}"

# Ollama (local, no API key needed)
brain:
  provider: ollama
  model: llama3.1:8b

# Mistral - no code change needed
brain:
  provider: mistral
  model: mistral-large-latest
  config:
    api_key: "{{secret.MISTRAL_API_KEY}}"
    base_url: "https://api.mistral.ai/v1"

# Groq - no code change needed
brain:
  provider: groq
  model: llama-3.3-70b-versatile
  config:
    api_key: "{{secret.GROQ_API_KEY}}"
    base_url: "https://api.groq.com/openai/v1"

# Together AI - no code change needed
brain:
  provider: together
  model: meta-llama/Llama-3-70b-chat-hf
  config:
    api_key: "{{secret.TOGETHER_API_KEY}}"
    base_url: "https://api.together.xyz/v1"
```

### Secrets Injection in Brain Config

API keys and other sensitive values in `brain.config` are resolved automatically via the expression engine. Use `{{secret.KEY_NAME}}` to reference a secret stored in the application's secret store.

```yaml
brain:
  provider: google
  model: gemini-2.0-flash
  config:
    api_key: "{{secret.MY_API_KEY}}"    # Resolved at runtime from the secret store
```

**How to store a secret:**

```bash
# Via CLI
llmos-bridge app secret set <app-name> MY_API_KEY "sk-..."

# Via API
PUT /applications/{app_id}/secrets/MY_API_KEY
Content-Type: application/json
{"value": "sk-..."}

# Via Dashboard
Applications → Select app → Secrets → Add Secret
```

Secrets are stored encrypted in the identity database and are **never exposed** in API responses, logs, or YAML output. They are resolved at runtime just before creating the LLM provider.

`{{secret.X}}` works everywhere in the YAML - not just in `brain.config`:

| Context | Example |
|---------|---------|
| `brain.config` | `api_key: "{{secret.OPENAI_KEY}}"` |
| `system_prompt` | `API key: {{secret.INTERNAL_KEY}}` |
| `variables` | `api_token: "{{secret.TOKEN}}"` |
| `constraints` | `api_key: "{{secret.KEY}}"` |
| Flow steps | `params: { token: "{{secret.TOKEN}}" }` |
| Triggers | `secret: "{{secret.WEBHOOK_SECRET}}"` |

## System Prompt

The `system_prompt` field defines the agent's behavior, personality, and instructions. It supports template expressions.

```yaml
agent:
  system_prompt: |
    You are a senior code reviewer.
    Workspace: {{workspace}}

    Previous review context:
    {{memory.last_review_summary}}

    Available languages: {{languages}}
```

### Best Practices

1. **Be specific** - Define the agent's role, capabilities, and constraints
2. **Reference tools** - Describe what each tool does so the LLM knows when to use them
3. **Use variables** - Inject dynamic context with `{{variable_name}}`
4. **Include memory** - Reference `{{memory.key}}` for persistent context
5. **Set guidelines** - Define rules (e.g., "never modify files outside workspace")

## Loop Configuration

The `loop:` block controls how the agent iterates.

```yaml
agent:
  loop:
    type: reactive                           # reactive | single_shot | continuous
    max_turns: 200                           # Max iterations (default: 200)
    stop_conditions:
      - "{{agent.no_tool_calls}}"            # Stop when no tools called
    on_tool_error: show_to_agent             # How to handle tool errors
    on_llm_error: retry                      # How to handle LLM errors
    retry:
      max_attempts: 3                        # Retry count (1-20)
      backoff: exponential                   # exponential | fixed | linear
    context:
      max_tokens: 200000                     # Context window size
      strategy: summarize                    # sliding_window | summarize | truncate
      keep_system_prompt: true
      keep_last_n_messages: 30
    planning:
      enabled: true                          # Enable planning mode
      batch_actions: true                    # Batch tool calls
      max_actions_per_batch: 20
      replan_on_failure: true
```

### Loop Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `type` | enum | `reactive` | `reactive`, `single_shot`, `continuous` |
| `max_turns` | int | `200` | Max iterations (1-10000) |
| `stop_conditions` | list | `["{{agent.no_tool_calls}}"]` | Expressions evaluated each turn; loop stops when any is true |
| `on_tool_error` | enum | `show_to_agent` | `show_to_agent`, `retry`, `fail`, `skip` |
| `on_llm_error` | enum | `retry` | `retry`, `fail` |
| `retry` | object | | Retry configuration (see below) |
| `context` | object | | Context window management (see [Context Management](context-management)) |
| `planning` | object | | Planning behavior (see below) |

### Loop Types

| Type | Behavior | Use Case |
|------|----------|----------|
| `reactive` | Plan → execute → observe → replan | Default, most flexible |
| `single_shot` | LLM acts once and returns | Simple Q&A, classification |
| `continuous` | Runs indefinitely, waits for triggers | Daemon/service mode |

### Error Handling

**Tool errors** (`on_tool_error`):

| Value | Behavior |
|-------|----------|
| `show_to_agent` | Show the error to the LLM so it can adapt (default) |
| `retry` | Retry the tool call automatically |
| `fail` | Stop the agent loop immediately |
| `skip` | Ignore the error and continue |

**LLM errors** (`on_llm_error`):

| Value | Behavior |
|-------|----------|
| `retry` | Retry with backoff (default) |
| `fail` | Stop immediately |

### Context Strategy

| Strategy | Behavior |
|----------|----------|
| `summarize` | Summarize older messages (default, recommended) |
| `sliding_window` | Drop oldest messages |
| `truncate` | Hard truncate at token limit |

See [Context Management](context-management) for advanced context configuration.

## Agent Identity (Multi-Agent)

When used in the `agents:` block (multi-agent mode), agents have additional identity fields:

```yaml
agents:
  - id: planner                     # Unique agent ID (required in multi-agent)
    role: coordinator               # coordinator | specialist | reviewer | observer
    expertise: [planning, analysis] # Areas of expertise
    preferred_node: ""              # Preferred cluster node (distributed mode)
    brain: { ... }
    system_prompt: "..."
    tools: [ ... ]
    loop: { ... }
```

### Agent Roles

| Role | Description |
|------|-------------|
| `coordinator` | Orchestrates other agents, delegates tasks |
| `specialist` | Focused on specific domains/tools |
| `reviewer` | Reviews and validates other agents' output |
| `observer` | Monitors without direct intervention |

See [Multi-Agent](multi-agent) for full multi-agent documentation.

## Complete Agent Example

```yaml
agent:
  id: coder
  role: specialist
  brain:
    provider: anthropic
    model: claude-sonnet-4-20250514
    temperature: 0.2
    max_tokens: 8192
    fallback:
      - model: claude-haiku-4-5-20251001
  system_prompt: |
    You are an expert coding assistant.
    Read files before modifying them.
    Run tests after changes.
    Never modify files outside {{workspace}}.
  tools:
    - module: filesystem
    - module: os_exec
      action: run_command
  loop:
    type: reactive
    max_turns: 30
    on_tool_error: show_to_agent
    retry:
      max_attempts: 3
      backoff: exponential
    context:
      strategy: summarize
      keep_last_n_messages: 20
    planning:
      enabled: true
      batch_actions: true
```
