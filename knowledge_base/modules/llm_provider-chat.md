---
id: llm_provider-chat
title: "llm_provider.chat (Chat)"
type: module-action
module: llm_provider
action: chat
fqn: llm_provider.chat
short_name: Chat
keywords: [llm_provider, chat, inference]
permissions: [llm_provider:chat]
risk_level: low
irreversible: false
require_approval: false
---

# llm_provider.chat (Chat)

## Description
Send a chat completion request to a configured LLM provider. Supports all standard parameters (temperature, max_tokens, top_p, stop, tools) plus provider-specific extras. Returns the model response with token usage.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `provider_id` | string | ✓ | — | Name of the configured provider instance to use. |
| `messages` | array | ✓ | — | Conversation messages. |
| `temperature` | number |  | — | Sampling temperature (0.0 = deterministic, 2.0 = creative). Overrides provider default. |
| `max_tokens` | integer |  | — | Maximum tokens to generate. Overrides provider default. |
| `top_p` | number |  | — | Nucleus sampling threshold. Overrides provider default. |
| `stop` | array |  | — | Stop sequences — generation stops when any is produced. |
| `tools` | array |  | — | Tool definitions (OpenAI function-calling format). |
| `tool_choice` | string |  | — | Tool selection strategy: 'auto', 'none', 'required', or a specific tool name. |
| `response_format` | object |  | — | Response format constraint (e.g. {'type': 'json_object'}). |
| `stream` | boolean |  | `False` | If True, return a streaming response. |
| `extra` | object |  | — | Provider-specific extra parameters passed through to the API. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: llm_provider
      actions: [chat]
```

## Safety
- Required permissions: `llm_provider:chat`
- Risk level: **low**
