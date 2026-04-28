---
id: brain-providers
title: "Brain & providers (LLM provider configuration)"
type: concept
keywords: [brain, provider, anthropic, openai, deepseek, groq, mistral, together, ollama, lm_studio, vllm, backend, api_key, base_url, temperature, max_tokens, native_tool_use, vision, context, model, claude_code, openai_compat]
related: [execution-modes, agent-spawn, capabilities]
source: packages/digitorn/core/app/schema.py
---

# Brain & providers -- LLM provider configuration

## What it is

Every agent in a Digitorn app has a `brain:` block that configures which LLM it uses, how it connects, and how it behaves. The brain supports two connection backends: **anthropic** (native Anthropic API) and **openai_compat** (OpenAI-compatible API, used by most providers).

## YAML reference

### Inline brain (full config embedded)

```yaml
agents:
  - id: main
    brain:
      provider: deepseek           # Provider hint (for auto base_url)
      model: deepseek-chat         # Model identifier
      backend: openai_compat       # openai_compat or anthropic
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
      temperature: 0.2
      max_tokens: 4096
      top_p: 0.9
      timeout: 60
      native_tool_use: true        # null = auto-detect
      vision: null                 # null = auto-detect from model name
      context:
        max_tokens: 128000
        strategy: summarize
        keep_recent: 10
        auto_compact: true
```

### Reference brain (points to named provider)

```yaml
modules:
  llm_provider:
    config:
      providers:
        deepseek_main:
          provider: deepseek
          model: deepseek-chat
          backend: openai_compat
          config:
            api_key: "{{secret.DEEPSEEK_API_KEY}}"
            base_url: "https://api.deepseek.com/v1"

agents:
  - id: main
    brain:
      provider_id: deepseek_main   # Reference the named provider
      temperature: 0.2             # Override settings
      max_tokens: 4096
```

## Brain fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `provider` | string | null | Provider hint: `anthropic`, `openai`, `deepseek`, `groq`, `mistral`, `together`, `ollama`, `lm_studio`, `vllm` |
| `model` | string | null | Model identifier (e.g., `deepseek-chat`, `claude-sonnet-4-20250514`) |
| `backend` | string | "openai_compat" | `openai_compat` or `anthropic` |
| `config` | dict | {} | Provider-specific config (api_key, base_url, etc.) |
| `provider_id` | string | null | Reference to a named provider (ignores provider/model/config) |
| `temperature` | float | null | Sampling temperature |
| `max_tokens` | int | null | Max tokens to generate per response |
| `top_p` | float | null | Nucleus sampling threshold |
| `timeout` | float | null | Request timeout in seconds |
| `native_tool_use` | bool | null | Override native tool calling detection |
| `vision` | bool | null | Override vision capability detection |
| `image_detail` | string | "auto" | Image resolution: `auto`, `low` (512px), `high` (native) |
| `max_images_per_turn` | int | 5 | Max images per turn (0 = unlimited) |
| `context` | ContextConfig | null | Per-brain context management (overrides execution.context) |

## Provider configurations

### Anthropic (Claude models)

```yaml
brain:
  provider: anthropic
  model: claude-sonnet-4-20250514
  backend: anthropic
  config:
    api_key: "{{secret.ANTHROPIC_API_KEY}}"
  temperature: 0.1
  max_tokens: 8192
```

### Anthropic via Claude Code OAuth token

The special `api_key: "claude-code"` reads the OAuth token from `~/.claude/.credentials.json`:

```yaml
brain:
  provider: anthropic
  model: claude-sonnet-4-20250514
  backend: anthropic
  config:
    api_key: "claude-code"
  max_tokens: 16384
```

This sends headers: `x-app: cli`, `anthropic-beta: oauth-2025-04-20,claude-code-20250219`. Has 15 retries with exponential backoff for rate limits. Token is cached in memory with expiry check.

### OpenAI

```yaml
brain:
  provider: openai
  model: gpt-4o
  backend: openai_compat
  config:
    api_key: "{{secret.OPENAI_API_KEY}}"
  temperature: 0.2
  max_tokens: 4096
```

### DeepSeek

```yaml
brain:
  provider: deepseek
  model: deepseek-chat
  backend: openai_compat
  config:
    api_key: "{{secret.DEEPSEEK_API_KEY}}"
    base_url: "https://api.deepseek.com/v1"
  temperature: 0.2
  max_tokens: 4096
  context:
    max_tokens: 128000
```

### Groq (fast inference)

```yaml
brain:
  provider: groq
  model: llama-3.3-70b-versatile
  backend: openai_compat
  config:
    api_key: "{{secret.GROQ_API_KEY}}"
    base_url: "https://api.groq.com/openai/v1"
  temperature: 0.2
  max_tokens: 4096
```

### Mistral

```yaml
brain:
  provider: mistral
  model: mistral-large-latest
  backend: openai_compat
  config:
    api_key: "{{secret.MISTRAL_API_KEY}}"
    base_url: "https://api.mistral.ai/v1"
  temperature: 0.2
```

### Together AI

```yaml
brain:
  provider: together
  model: meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo
  backend: openai_compat
  config:
    api_key: "{{secret.TOGETHER_API_KEY}}"
    base_url: "https://api.together.xyz/v1"
  temperature: 0.2
```

### Ollama (local)

```yaml
brain:
  provider: ollama
  model: qwen2.5-coder:32b
  backend: openai_compat
  config:
    base_url: "http://localhost:11434/v1"
  native_tool_use: true      # Must be explicit for Ollama
  temperature: 0.1
```

### LM Studio (local)

```yaml
brain:
  provider: lm_studio
  model: local-model
  backend: openai_compat
  config:
    base_url: "http://localhost:1234/v1"
  native_tool_use: false     # Most local models need text-based
```

### vLLM (self-hosted)

```yaml
brain:
  provider: vllm
  model: meta-llama/Llama-3.1-70B-Instruct
  backend: openai_compat
  config:
    base_url: "http://gpu-server:8000/v1"
    api_key: "token-or-empty"
  native_tool_use: true
```

## API key patterns

### From secrets (recommended)

```yaml
config:
  api_key: "{{secret.DEEPSEEK_API_KEY}}"
```

Resolved at compile time from the daemon's secret store or the app's configured credentials.

### From environment variables

```yaml
config:
  api_key: "{{env.OPENAI_API_KEY}}"
```

### Claude Code OAuth

```yaml
config:
  api_key: "claude-code"
```

### Direct (not recommended for production)

```yaml
config:
  api_key: "sk-actual-key-here"
```

## native_tool_use

Controls how the agent calls tools:

| Value | Behavior |
|-------|----------|
| `null` (default) | Auto-detect from provider. Anthropic/OpenAI/DeepSeek = native. Ollama = text-based. |
| `true` | Force native OpenAI-style `tool_calls` |
| `false` | Force text-based tool calling (JSON in text output, parsed by runtime) |

Set explicitly for local models where auto-detection may be wrong.

## Context configuration

Per-brain or per-app context management:

```yaml
context:
  max_tokens: 128000          # 0 = auto-detect from provider
  strategy: summarize         # truncate or summarize
  keep_recent: 10             # Messages to keep during compaction
  compression_trigger: 0.75   # Token pressure ratio to trigger (0.0-1.0)
  auto_compact: true          # Enable automatic compaction
  summary_max_tokens: 1024    # Max tokens for the summary
  output_reserved: 4096       # Tokens reserved for output
  summary_brain:              # Optional: use a cheaper model for summaries
    provider: deepseek
    model: deepseek-chat
    backend: openai_compat
    config:
      api_key: "{{secret.DEEPSEEK_API_KEY}}"
      base_url: "https://api.deepseek.com/v1"
```

### Compaction strategies

| Strategy | Behavior |
|----------|----------|
| `truncate` | Drop old messages, keep system + last N. Fast, no LLM call. |
| `summarize` | Use LLM to summarize old messages, keep summary + last N. Smart, costs 1 LLM call. |

## Vision support

Auto-detected from model name for: Claude (Sonnet/Opus/Haiku), GPT-4o, GPT-4-turbo, Gemini, LLaVA, DeepSeek-VL, Pixtral, Qwen-VL.

Providers without vision (e.g., DeepSeek-chat) auto-convert images to `[Image: description]` text.

```yaml
brain:
  provider: anthropic
  model: claude-sonnet-4-20250514
  vision: true              # Explicit (auto-detected anyway)
  image_detail: auto        # auto, low (512px), high (native res)
  max_images_per_turn: 5    # 0 = unlimited
```

## Per-agent model strategy

Different agents can use different models to optimize cost vs quality:

```yaml
agents:
  - id: coordinator
    brain:
      provider: anthropic
      model: claude-sonnet-4-20250514      # Strong model for orchestration
      config:
        api_key: "claude-code"

  - id: explorer
    brain:
      provider: deepseek
      model: deepseek-chat            # Cheap model for bulk search
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
      temperature: 0.0

  - id: writer
    brain:
      provider: anthropic
      model: claude-sonnet-4-20250514      # Strong model for quality output
      config:
        api_key: "claude-code"
      temperature: 0.4
```

## Fallback brain - automatic billing failover

When a provider returns a billing/credit error (402, "Insufficient Balance"),
Digitorn automatically switches to a fallback brain if configured:

```yaml
brain:
  provider: deepseek
  model: deepseek-chat
  backend: openai_compat
  config:
    api_key: "{{secret.DEEPSEEK_API_KEY}}"
  fallback:
    provider: anthropic
    model: claude-haiku-4-5
    config:
      api_key: "claude-code"
```

### How it works

1. Agent calls primary provider → 402 / "Insufficient Balance"
2. Daemon detects billing error
3. Daemon switches to the fallback brain for the rest of the turn
4. Agent continues without interruption
5. Next turn retries the primary provider first

### Fallback rules

- Per-agent - each agent can have its own fallback
- Supports all brain fields (provider, model, config, temperature, etc.)
- If no fallback configured → error raised to user
- Temporary - next turn retries primary first

### Common patterns

**Expensive → cheap:**
```yaml
fallback:
  provider: anthropic
  model: claude-haiku-4-5
  config: { api_key: "claude-code" }
```

**Cloud → local:**
```yaml
fallback:
  provider: ollama
  model: qwen2.5-coder
  config: { base_url: "http://localhost:11434/v1" }
```

**Same provider, smaller model:**
```yaml
brain:
  provider: anthropic
  model: claude-sonnet-4-5
  config: { api_key: "{{secret.ANTHROPIC_KEY}}" }
  fallback:
    provider: anthropic
    model: claude-haiku-4-5
    config: { api_key: "claude-code" }
```
