---
id: llm_provider-configure
title: "llm_provider.configure (Configure)"
type: module-action
module: llm_provider
action: configure
fqn: llm_provider.configure
short_name: Configure
keywords: [llm_provider, configure, configuration, lifecycle]
permissions: [llm_provider:admin]
risk_level: medium
irreversible: false
require_approval: false
---

# llm_provider.configure (Configure)

## Description
Register a named LLM provider instance. Supports Anthropic (native SDK) and any OpenAI-compatible API (OpenAI, DeepSeek, Groq, Mistral, Together, Ollama, vLLM, LM Studio). Provider instances can be referenced by agents in their brain configuration.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `provider_id` | string | ✓ | — | Unique name for this provider instance (e.g. 'coordinator_brain', 'worker_brain', 'deepseek_coder'). |
| `backend` | string |  | `openai_compat` | Provider backend: 'anthropic' (native Anthropic SDK) or 'openai_compat' (any OpenAI-compatible API: OpenAI, DeepSeek, Groq, Mistral, Together, Ollama, vLLM, LM Studio, etc.). |
| `model` | string | ✓ | — | Model identifier (e.g. 'claude-sonnet-4-20250514', 'deepseek-chat', 'gpt-4o'). |
| `api_key` | string |  | `` | API key. Leave empty for local providers (Ollama, vLLM) or if set via environment variable. |
| `base_url` | string |  | — | API base URL. Auto-resolved for known providers (openai, deepseek, groq, mistral, together, ollama, lm_studio, vllm). Set explicitly for custom endpoints. |
| `provider_hint` | string |  | — | Hint for auto-resolving base_url: 'openai', 'deepseek', 'groq', 'mistral', 'together', 'ollama', 'lm_studio', 'vllm'. |
| `timeout` | number |  | `120.0` | Request timeout in seconds. |
| `max_retries` | integer |  | `2` | Max automatic retries on transient errors. |
| `default_params` | object |  | — | Default generation parameters applied to every request (temperature, max_tokens, top_p, etc.). Per-request overrides take precedence. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: llm_provider
      actions: [configure]
```

## Safety
- Required permissions: `llm_provider:admin`
- Risk level: **medium**
