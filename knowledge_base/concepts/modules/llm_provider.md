---
id: module-concept-llm_provider
title: "llm_provider module - overview"
type: module-concept
module: llm_provider
isolation: shared
keywords: [llm_provider, llm_provider-module, configure, chat, remove, list_providers, get_provider_info, update_defaults]
version: 1.0.0
---

# `llm_provider` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `1.0.0`
- **Actions**: 6 visible, 0 internal

## Description (from class docstring)

LLMProviderModule - unified access to all LLM providers.

Named provider instances (like named database connections) can be configured
at startup via app YAML or dynamically at runtime. Each instance wraps a
specific backend (Anthropic native, OpenAI-compatible) and model with its
own default parameters.

Actions:
    configure       Register a named provider instance
    chat            Send a chat completion request
    remove          Remove a provider instance
    list_providers  List all configured provider instances
    get_provider_info   Get metadata about a provider instance
    update_defaults     Update default generation parameters

> Class-level summary: Unified LLM provider module.

    Manages named provider instances and dispatches chat requests
    to the appropriate backend. Supports Anthropic (native SDK) and
    any OpenAI-compatible API (OpenAI, DeepSeek, Groq, Mistral,
    Together, Ollama, vLLM, LM Studio, etc.).

## Configuration

Set under `modules.llm_provider.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon. |
| `providers` | dict |  | `{}` | Named LLM provider instances - free-form per backend. |
| `default_provider` | str \| None |  | `None` | Name of the default provider used when agents don't pick one. |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `configure` | `Configure` |  | medium | Register a named LLM provider instance. Supports Anthropic (native SDK) and any OpenAI-compatible API (OpenAI, DeepSe... |
| `chat` | `Chat` |  | low | Send a chat completion request to a configured LLM provider. Supports all standard parameters (temperature, max_token... |
| `remove` | `Remove` |  | low | Remove a configured LLM provider instance and release its resources. |
| `list_providers` | `ListProviders` |  | low | List all configured LLM provider instances with their models and backends. |
| `get_provider_info` | `GetProviderInfo` |  | low | Get detailed metadata about a configured provider instance including capabilities. |
| `update_defaults` | `UpdateDefaults` |  | low | Update default generation parameters (temperature, max_tokens, top_p) for an existing provider instance. These defaul... |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: llm_provider
      actions: [configure, chat, remove, list_providers, get_provider_info, update_defaults]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {llm_provider: [configure, chat, remove, list_providers, get_provider_info]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/llm_provider-*.md`.
