# LLM Provider Module

Unified access to all major LLM providers through named provider instances.

## Key Features

- **Named instances** — configure multiple providers like named database connections (`coordinator_brain`, `worker_brain`, `coder_brain`)
- **Two backends** — Anthropic (native SDK) and OpenAI-compatible (covers OpenAI, DeepSeek, Groq, Mistral, Together, Ollama, vLLM, LM Studio)
- **Auto-resolved URLs** — known providers (deepseek, groq, ollama...) resolve automatically from a `provider_hint`
- **Default params** — set temperature, max_tokens, top_p per instance; override per request
- **Tool use** — full function-calling support across all backends
- **Streaming** — async streaming for both Anthropic and OpenAI-compatible providers
- **State persistence** — provider configs survive daemon restarts (API keys excluded)

## Supported Providers

| Provider | Backend | Auto-resolved |
|----------|---------|---------------|
| Anthropic (Claude) | `anthropic` | Yes |
| OpenAI (GPT) | `openai_compat` | Yes |
| DeepSeek | `openai_compat` | Yes |
| Groq | `openai_compat` | Yes |
| Mistral | `openai_compat` | Yes |
| Together | `openai_compat` | Yes |
| Ollama (local) | `openai_compat` | Yes |
| vLLM (local) | `openai_compat` | Yes |
| LM Studio (local) | `openai_compat` | Yes |
| Any OpenAI-compat | `openai_compat` | Via `base_url` |

## Actions (6)

| Action | Risk | Description |
|--------|------|-------------|
| `configure` | medium | Register a named provider instance |
| `chat` | low | Send a chat completion request |
| `remove` | low | Remove a provider instance |
| `list_providers` | low | List all configured instances |
| `get_provider_info` | low | Get provider metadata and capabilities |
| `update_defaults` | low | Update default generation parameters |

## App YAML Example

```yaml
modules:
  llm_provider:
    config:
      providers:
        coordinator_brain:
          backend: anthropic
          model: claude-sonnet-4-20250514
          api_key: "{{secret.ANTHROPIC_API_KEY}}"
          temperature: 0.2
          max_tokens: 8192
        worker_brain:
          backend: openai_compat
          provider_hint: deepseek
          model: deepseek-chat
          api_key: "{{env.DEEPSEEK_API_KEY}}"
          temperature: 0.3
          max_tokens: 4096
        local_coder:
          backend: openai_compat
          provider_hint: ollama
          model: codellama:34b
          temperature: 0.1
```

## Agent Brain Configuration

In the YAML agent definition, the `brain` section references a provider:

```yaml
agents:
  - id: coordinator
    role: coordinator
    brain:
      provider: deepseek
      model: deepseek-chat
      temperature: 0.2
      max_tokens: 8192
      timeout: 120.0
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
```

The `llm_provider` module handles this configuration and exposes the model parameters to the `context_manager` module (for runtime parameter access).
