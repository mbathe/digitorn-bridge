---
id: yaml-schema-agentbrain
title: "AgentBrain — YAML schema reference"
type: schema-reference
model: AgentBrain
is_root: false
keywords: [agentbrain, backend, config, context, fallback, image_detail, image_generation, max_images_per_turn, max_tokens, model, native_tool_use]
---

# AgentBrain

## Description
LLM brain configuration for an agent.

Two modes:

1. **Inline** — full provider config embedded in the agent::

brain:
provider: deepseek
model: deepseek-chat
temperature: 0.2
config:
api_key: "{{secret.DEEPSEEK_API_KEY}}"
base_url: "https://api.deepseek.com/v1"

2. **Reference** — points to a named provider in ``modules.llm_provider``::

brain:
provider_id: deepseek_main
temperature: 0.2

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `provider_id` | str \| null |  | `None` | Reference to a named provider declared in modules.llm_provider.config.providers. If set, provider/model/config are ignored. |
| `provider` | str \| null |  | `None` | Provider hint for auto-resolving base URL. Known values: anthropic, openai, deepseek, groq, mistral, together, ollama, lm_studio, vllm, google-gemini, gemini, xai, grok, cerebras, perplexity, fireworks. |
| `model` | str \| null |  | `None` | Model identifier (e.g. 'deepseek-chat', 'claude-sonnet-4-20250514'). |
| `backend` | 'openai_compat' \| 'anthropic' |  | `'openai_compat'` | Provider backend: 'anthropic' or 'openai_compat'. |
| `config` | dict[str, any] |  | `{}` | Provider-specific config (api_key, base_url, etc.). |
| `temperature` | float \| null |  | `None` | Sampling temperature. |
| `max_tokens` | int \| null |  | `None` | Max tokens to generate. |
| `top_p` | float \| null |  | `None` | Nucleus sampling threshold. |
| `timeout` | float \| null |  | `None` | Request timeout in seconds. |
| `native_tool_use` | bool \| null |  | `None` | Override native tool calling detection. By default, auto-detected from provider (e.g. Ollama defaults to text-based). Set to true to force native OpenAI-style tool_calls (e.g. qwen2.5-coder on Ollama). Set to false to force text-based tool calling. |
| `context` | [ContextConfig](ContextConfig.md) \| null |  | `None` | Context window management for this brain. If not set, inherits from execution.context. Useful in multi-agent apps where each brain uses a different model. |
| `fallback` | [AgentBrain](AgentBrain.md) \| null |  | `None` | Fallback brain used when the primary provider returns a billing or credit error (402, insufficient balance). Lets apps gracefully degrade to a cheaper/free model instead of failing. Example:   fallback:     provider: anthropic     model: claude-haiku-4-5     config:       api_key: "claude-code" |
| `vision` | bool \| null |  | `None` | Whether this model supports image input (vision). null = auto-detect from model name. true = force enabled. false = convert images to text descriptions. |
| `image_generation` | bool |  | `False` | Whether this model can generate images. If true, the framework handles image output in tool results and SSE events. Models like DALL-E, Stable Diffusion via MCP. |
| `image_detail` | str |  | `'auto'` | Image resolution for vision. 'auto' = provider decides. 'low' = 512px (cheaper, faster). 'high' = native resolution (more accurate, more tokens). |
| `max_images_per_turn` | int |  | `5` | Max images sent to the model per turn (0 = unlimited). |

## Linked models
- [ContextConfig](ContextConfig.md)

## Strictness
- `extra: forbid` — unknown keys cause a validation error
