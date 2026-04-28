# LLM Provider - Action Reference

## configure

Register a named LLM provider instance.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `provider_id` | string | Yes | Unique name (e.g. `coordinator_brain`) |
| `backend` | string | No | `anthropic` or `openai_compat` (default) |
| `model` | string | Yes | Model identifier |
| `api_key` | string | No | API key (empty for local providers) |
| `base_url` | string | No | API base URL (auto-resolved for known providers) |
| `provider_hint` | string | No | Auto-resolve URL: `openai`, `deepseek`, `groq`, `mistral`, `together`, `ollama`, `lm_studio`, `vllm` |
| `timeout` | float | No | Request timeout in seconds (default: 120) |
| `max_retries` | int | No | Max retries (default: 2) |
| `default_params` | object | No | Default generation params (temperature, max_tokens, etc.) |

**Permissions:** `llm_provider:admin` · **Risk:** medium

---

## chat

Send a chat completion request.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `provider_id` | string | Yes | Provider instance to use |
| `messages` | array | Yes | Conversation messages (`role`, `content`) |
| `temperature` | float | No | Sampling temperature (0.0–2.0) |
| `max_tokens` | int | No | Max tokens to generate |
| `top_p` | float | No | Nucleus sampling (0.0–1.0) |
| `stop` | array | No | Stop sequences |
| `tools` | array | No | Tool definitions (OpenAI format) |
| `tool_choice` | string | No | `auto`, `none`, `required` |
| `response_format` | object | No | e.g. `{"type": "json_object"}` |
| `stream` | boolean | No | Enable streaming (default: false) |
| `extra` | object | No | Provider-specific extra params |

**Permissions:** `llm_provider:chat` · **Risk:** low

---

## remove

Remove a configured provider instance and release resources.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `provider_id` | string | Yes | Provider instance to remove |

**Permissions:** `llm_provider:admin` · **Risk:** low

---

## list_providers

List all configured provider instances. No parameters.

**Permissions:** `llm_provider:read` · **Risk:** low

---

## get_provider_info

Get detailed metadata about a provider instance.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `provider_id` | string | Yes | Provider instance name |

**Permissions:** `llm_provider:read` · **Risk:** low

---

## update_defaults

Update default generation parameters for a provider.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `provider_id` | string | Yes | Provider instance to update |
| `temperature` | float | No | Default temperature |
| `max_tokens` | int | No | Default max tokens |
| `top_p` | float | No | Default top_p |
| `extra` | object | No | Additional default params |

**Permissions:** `llm_provider:admin` · **Risk:** low
