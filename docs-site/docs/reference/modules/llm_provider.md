---
id: llm_provider
title: llm_provider Module
sidebar_label: llm_provider
sidebar_position: 7
description: System module - manages LLM provider connections, auto-configured from agent brains.
---

# llm_provider

System module that manages LLM provider instances. Auto-configured
from each agent's `brain:` block. Supports the Anthropic native
SDK and any OpenAI-compatible API.

| Property | Value |
|----------|-------|
| Module id | `llm_provider` |
| Version | `1.0.0` |
| Type | system (auto-loaded, hidden from agents) |
| Pip deps | `openai` (OpenAI-compatible), `anthropic` (Anthropic native) |

## Role in the architecture

1. **Auto-configures** from each agent's `brain:` definition
   in `agents[]`.
2. **Resolves provider URLs** - `provider: deepseek` →
   `https://api.deepseek.com/v1`.
3. **Manages connections** - async HTTP clients with pooling +
   per-provider retry policy.
4. **Handles tool calling** - detects native vs. text-based
   tool calling per model.
5. **Normalises responses** - same shape regardless of provider.

## Recognised provider names

`provider:` shortcut → base URL + backend kind. Custom
providers work by setting `base_url` directly.

| `provider:` | Base URL | `backend` |
|-------------|----------|-----------|
| `openai` | `https://api.openai.com/v1` | `openai_compat` |
| `deepseek` | `https://api.deepseek.com/v1` | `openai_compat` |
| `anthropic` | `https://api.anthropic.com` | `anthropic` |
| `groq` | `https://api.groq.com/openai/v1` | `openai_compat` |
| `mistral` | `https://api.mistral.ai/v1` | `openai_compat` |
| `together` | `https://api.together.xyz/v1` | `openai_compat` |
| `ollama` | `http://localhost:11434/v1` | `openai_compat` |
| `lm_studio` | `http://localhost:1234/v1` | `openai_compat` |
| `vllm` | `http://localhost:8000/v1` | `openai_compat` |
| `openrouter` | `https://openrouter.ai/api/v1` | `openai_compat` |

> **`AgentBrain.backend`** (in) is a literal of
> **three values**: `"openai_compat"`, `"anthropic"`, or
> `"github_copilot"`. Despite what older docs / canvas widgets
> may show, `"native"` and `"anthropic_compat"` are **not**
> valid backends.

## Claude Code OAuth token

The Anthropic provider accepts the literal string
`"claude-code"` as `api_key`. When set:

- Reads the OAuth token from
  `~/.claude/.credentials.json` (`claudeAiOauth.accessToken`).
- Sends headers `x-app: cli` + `anthropic-beta:
  oauth-2025-04-20,claude-code-20250219`.
- Auto-refreshes on 401.
- 15 retries with exponential backoff for rate limits.

```yaml
brain:
  provider: anthropic
  model: claude-sonnet-4-5
  backend: anthropic
  config:
    api_key: "claude-code"
```

## Billing failover (`brain.fallback`)

Every brain accepts an optional nested `fallback:` (type
`AgentBrain`). It kicks in on `402 / "Insufficient Balance" /
"credit"` errors.

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
    backend: anthropic
    config:
      api_key: "claude-code"
```

The runtime switches transparently for the current request and
retries the primary on the next turn. Wired
into `AgentContext._fallback_brain`; dispatched by
`_handle_llm_error`.

## Text-based tool-call recovery

When a model emits malformed or non-native tool calls, the
multi-format parser recovers them from text:

1. Native JSON tool-call format.
2. XML-wrapped tool calls.
3. Markdown code-block JSON.
4. Inline JSON with function names.
5. Unicode quote normalisation.

This makes Digitorn compatible with local models (Ollama,
vLLM) that have imperfect tool calling.

> **Local models with native tool calling** (e.g.
> `qwen2.5-coder` on Ollama): set `native_tool_use: true` on
> the brain to bypass text recovery.

## Streaming JSON recovery (anthropic provider)

The Anthropic provider accumulates `input_json_delta` events
during streaming. If the SDK's `get_final_message` returns
empty tool input, the provider reconstructs from accumulated
fragments via `_recover_tool_json` (handles truncated JSON by
closing braces / regex extraction). Pair with
`max_tokens: 16384` to avoid truncation.

## The 6 internal actions

 All `permissions=["llm_provider:admin"]` or
`["llm_provider:read"]` (hidden from agents - only the
runtime calls them).

| Tool | Risk | Purpose |
|------|:----:|---------|
| `llm_provider.configure` | medium | Register a named provider instance. |
| `llm_provider.chat` | low | Send a chat completion request. |
| `llm_provider.remove` | low | Remove a provider + release its resources. |
| `llm_provider.list_providers` | low | List configured providers + backends. |
| `llm_provider.get_provider_info` | low | Provider metadata + capabilities (tool support, max tokens, vision). |
| `llm_provider.update_defaults` | low | Update default generation params (temperature, max_tokens, top_p). |

## Agent-side YAML

The module is implicit - never declare it in
`tools.modules:`. Agents reference providers through
`brain:`:

```yaml
agents:
  - id: assistant
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      temperature: 0.2
      max_tokens: 4096
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
```

## Cross-references

- Agent / brain reference (`AgentBrain` schema):
  [Agents](../../language/03-agents.md)
- Credentials vault for provider keys:
  [credentials.md](../../reference/runtime/credentials.md)
- Examples covering DeepSeek, OpenAI, Anthropic, Ollama,
  Groq, multi-agent: [Examples](../../language/15-examples.md)
