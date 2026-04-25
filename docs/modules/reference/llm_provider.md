---
id: llm_provider
title: LLM Provider Module
sidebar_label: llm_provider
sidebar_position: 7
description: System module -- manages LLM provider connections, auto-configuration from agent brain definitions.
---

# llm_provider

System module that manages LLM provider instances. Automatically configured from agent `brain:` definitions in YAML. Supports any OpenAI-compatible API and the Anthropic native SDK.

| Property | Value |
|----------|-------|
| **Module ID** | `llm_provider` |
| **Version** | `1.0.0` |
| **Type** | system (auto-loaded, hidden from agents) |
| **Dependencies** | `openai` (for OpenAI-compatible providers), `anthropic` (for Anthropic) |

---

## Role in the Architecture

The LLM provider module is the bridge between Digitorn agents and LLM APIs. It:

1. **Auto-configures** from the `brain:` section of each agent definition.
2. **Resolves provider URLs** automatically -- `provider: deepseek` maps to `https://api.deepseek.com/v1`.
3. **Manages connections** -- creates and reuses async HTTP clients with connection pooling.
4. **Handles tool calling** -- detects whether the model supports native tool calling or requires text-based recovery.
5. **Normalizes responses** -- provides a consistent response format regardless of provider.

---

## Supported Providers

The module auto-resolves base URLs for common providers:

| Provider | Base URL | Backend |
|----------|----------|---------|
| `openai` | `https://api.openai.com/v1` | openai_compat |
| `deepseek` | `https://api.deepseek.com/v1` | openai_compat |
| `anthropic` | `https://api.anthropic.com` | anthropic |
| `groq` | `https://api.groq.com/openai/v1` | openai_compat |
| `mistral` | `https://api.mistral.ai/v1` | openai_compat |
| `together` | `https://api.together.xyz/v1` | openai_compat |
| `ollama` | `http://localhost:11434/v1` | openai_compat |
| `lm_studio` | `http://localhost:1234/v1` | openai_compat |
| `vllm` | `http://localhost:8000/v1` | openai_compat |
| `openrouter` | `https://openrouter.ai/api/v1` | openai_compat |

Custom providers work by specifying `base_url` directly in the brain config.

### Claude Code OAuth token

The Anthropic provider accepts the literal string `"claude-code"` as an API key. When set, it reads the OAuth token from `~/.claude/.credentials.json` (`claudeAiOauth.accessToken`), sends the headers `x-app: cli` and `anthropic-beta: oauth-2025-04-20,claude-code-20250219`, and auto-refreshes on 401. 15 retries with exponential backoff for rate limits.

```yaml
brain:
  provider: anthropic
  model: claude-sonnet-4-5
  config:
    api_key: "claude-code"
```
### Billing failover (`brain.fallback`)

Every brain accepts an optional nested `fallback:` (type `AgentBrain`) that kicks in on 402 / "Insufficient Balance" / "credit" errors:

```yaml
brain:
  provider: deepseek
  model: deepseek-chat
  config:
    api_key: "{{env.DEEPSEEK_API_KEY}}"
  fallback:
    provider: anthropic
    model: claude-haiku-4-5
    config:
      api_key: "claude-code"
```
The runtime switches transparently for the current request and retries the primary on the next turn. Wired in `bootstrap.py` into `AgentContext._fallback_brain`; dispatched by `_handle_llm_error` in `runtime/agent_loop.py`.

---

## Configuration

The module is configured implicitly through agent brain definitions:

```yaml
agents:
  - id: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      temperature: 0.2
      max_tokens: 4096
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
```
No explicit `modules: llm_provider:` declaration is needed.

---

## Text-Based Tool Calling Recovery

When a model does not support native tool calling (or produces malformed tool calls), the module includes a multi-format parser that recovers tool calls from text:

1. Native JSON tool call format
2. XML-wrapped tool calls
3. Markdown code block JSON
4. Inline JSON with function names
5. Unicode quote normalization

This makes Digitorn compatible with local models (Ollama, vLLM) that have imperfect tool calling support.

> **Note**: Some local models (e.g., `qwen2.5-coder` on Ollama) support native tool calling. Set `native_tool_use: true` in the brain config to bypass text-based recovery and use the native API format. See [Agent Configuration](../../app-language/03-agents.md#overriding-tool-calling-mode).

---

## Actions (6)

These actions are for internal use and are hidden from agents:

| Action | Risk | Description |
|--------|------|-------------|
| `configure` | medium | Register a named provider instance |
| `chat` | low | Send a chat completion request |
| `remove` | low | Remove a provider instance |
| `list_providers` | low | List configured providers |
| `get_provider_info` | low | Return metadata + capabilities for one provider (tool support, max tokens, vision) |
| `update_defaults` | low | Update default generation parameters |
