---
id: agents
---

# Agents

An agent is an LLM with a brain (provider configuration), a system prompt, and a role. Every app has at least one agent defined in the `agents:` list.

## Agent Definition

```yaml
agents:
  - id: assistant
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
    system_prompt: |
      You are a helpful assistant.
      Workspace: {{workspace}}
```
### Agent Fields

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `id` | string | *required* | Unique agent identifier within the app |
| `role` | string | `"worker"` | Agent role: `coordinator`, `worker`, `assistant`, or custom |
| `brain` | AgentBrain | *required* | LLM provider configuration |
| `system_prompt` | string | `""` | System prompt injected at conversation start |
| `plan_first` | bool | `true` | Guide the agent to explain its plan before executing tools |

## Plan First

When enabled, communication guidelines are injected into the system prompt to encourage the LLM to explain what it's about to do before calling tools. This helps the user understand what's happening — especially since tool parameters and raw results are not shown directly.

This is **prompt-level guidance only** — the runtime never blocks or intercepts tool calls. The LLM remains free to work as it sees fit. How well the model follows these guidelines depends on the model itself (some models like DeepSeek never produce text alongside tool calls).

```yaml
agents:
  # Default: guidelines injected to encourage explanation
  - id: assistant
    plan_first: true
    brain: ...

  # No guidelines: agent works silently
  - id: background-worker
    plan_first: false
    brain: ...
```
Set `plan_first: false` for agents where explanation is unnecessary (background workers, pipelines, automated tasks).

> **Note**: Regardless of `plan_first`, the CLI always shows real-time tool activity (`> Listing .`, `> Reading file.py`, etc.) so the user is never completely in the dark.

## Brain Configuration

The `brain:` block configures the LLM provider and model. Two modes are supported.

### Inline Mode (recommended)

Full provider config embedded in the agent:

```yaml
brain:
  provider: deepseek         # Provider hint (for base URL resolution)
  model: deepseek-chat       # Model identifier
  backend: openai_compat     # Backend: 'openai_compat' (default) or 'anthropic'
  config:                    # Provider-specific config
    api_key: "{{env.DEEPSEEK_API_KEY}}"
    base_url: "https://api.deepseek.com/v1"  # Optional if provider hint is set
  temperature: 0.2           # Sampling temperature
  max_tokens: 8192           # Max output tokens
  top_p: 1.0                 # Nucleus sampling
  context:                   # Per-brain context management (optional)
    max_tokens: 131072
    strategy: summarize
```
### Reference Mode

Points to a named provider in `modules.llm_provider.config.providers`:

```yaml
brain:
  provider_id: my_deepseek_provider
  temperature: 0.2
```
### Brain Fields

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `provider_id` | string | `null` | Reference to a named provider (reference mode) |
| `provider` | string | `null` | Provider hint for base URL resolution (inline mode) |
| `model` | string | `null` | Model identifier |
| `backend` | string | `"openai_compat"` | Backend: `openai_compat` or `anthropic` |
| `config` | dict | `{}` | Provider-specific config (`api_key`, `base_url`, etc.) |
| `temperature` | float | `null` | Sampling temperature (0-2) |
| `max_tokens` | int | `null` | Max tokens to generate |
| `top_p` | float | `null` | Nucleus sampling threshold (0-1) |
| `timeout` | float | `null` | Request timeout in seconds |
| `native_tool_use` | bool | `null` | Override native tool calling detection. `true` = force native, `false` = force text-based, `null` = auto-detect |
| `context` | ContextConfig | `null` | Per-brain context management (overrides `execution.context`) |

### Supported Providers

All providers use the OpenAI-compatible API format (`openai_compat` backend) unless noted.

| Provider | Provider Hint | Default Base URL | Native Tool Use |
| -------- | ------------- | ---------------- | --------------- |
| OpenAI | `openai` | `https://api.openai.com/v1` | Yes |
| Anthropic | `anthropic` | `https://api.anthropic.com/v1` | Yes |
| DeepSeek | `deepseek` | `https://api.deepseek.com/v1` | Yes |
| Groq | `groq` | `https://api.groq.com/openai/v1` | Yes |
| Mistral | `mistral` | `https://api.mistral.ai/v1` | Yes |
| Together | `together` | `https://api.together.xyz/v1` | Yes |
| Ollama | `ollama` | `http://localhost:11434/v1` | **No** (text-based) |
| LM Studio | `lm_studio` | `http://localhost:1234/v1` | **No** (text-based) |
| vLLM | `vllm` | `http://localhost:8000/v1` | **No** (text-based) |

When the provider hint is set, the `base_url` is auto-resolved. You can always override it in `config.base_url`.

### Native vs Text-Based Tool Calling

Digitorn automatically detects whether a provider supports native tool calling:

- **Native** (OpenAI, DeepSeek, Groq, Mistral, Together): Tools are passed via the API `tools=` parameter. The LLM generates structured tool calls natively.

- **Text-based** (Ollama, LM Studio, vLLM): Tool schemas are injected into the system prompt. The LLM generates tool calls as text (e.g., `{tool_call}{"name": "...", "arguments": {...}}</tool_call>`), and Digitorn parses them.

This is fully automatic — you don't need to configure anything. The same YAML works with any provider.

#### Overriding Tool Calling Mode

Some local models (e.g., `qwen2.5-coder` on Ollama) support native tool calling even though their provider defaults to text-based. Use `native_tool_use` to override the auto-detection:

```yaml
brain:
  provider: ollama
  model: qwen2.5-coder:7b
  native_tool_use: true          # Force native tool calling
  config:
    base_url: "http://localhost:11434/v1"
```
| Value | Behavior |
| ----- | -------- |
| `true` | Force native mode — tools passed via API `tools=` parameter |
| `false` | Force text-based mode — tools injected in system prompt |
| `null` (default) | Auto-detect from provider hint |

> **Tip**: If your local model supports the OpenAI tool calling format (returns `tool_calls` in the response), set `native_tool_use: true` for significantly better reliability.

### Tool Call Recovery

Even with native tool calling, LLMs sometimes generate malformed tool calls. Digitorn handles this robustly:

1. **Llama native format**: `<function=name{...}</function>` — parsed via regex
2. **XML format**: `{tool_call}{...}</tool_call>` — parsed via regex
3. **Raw JSON**: `{"name": "...", "arguments": {...}}` — extracted via brace matching
4. **Markdown JSON**: ` ```json {...} ``` ` — extracted from code blocks
5. **Smart quotes**: Unicode curly quotes (`""''`) normalized to ASCII before parsing
6. **API errors**: Groq `tool_use_failed` errors with `failed_generation` are recovered

### Provider Examples

```yaml
# DeepSeek (cloud, native tool use)
brain:
  provider: deepseek
  model: deepseek-chat
  backend: openai_compat
  config:
    api_key: "{{env.DEEPSEEK_API_KEY}}"

# Groq (cloud, fast inference, native tool use)
brain:
  provider: groq
  model: llama-3.3-70b-versatile
  backend: openai_compat
  config:
    api_key: "{{env.GROQ_API_KEY}}"
    base_url: "https://api.groq.com/openai/v1"

# Ollama (local, text-based by default)
brain:
  provider: ollama
  model: qwen2.5:14b-instruct-q4_K_M
  backend: openai_compat
  config:
    base_url: "http://localhost:11434/v1"
  context:
    max_tokens: 8000
    output_reserved: 1000
    strategy: truncate
    keep_recent: 6
    compression_trigger: 0.60
    auto_compact: true

# Ollama with native tool calling (for models that support it)
brain:
  provider: ollama
  model: qwen2.5-coder:7b
  native_tool_use: true
  backend: openai_compat
  config:
    base_url: "http://localhost:11434/v1"

# Any OpenAI-compatible endpoint
brain:
  provider: custom
  model: my-fine-tuned-model
  backend: openai_compat
  config:
    api_key: "{{env.CUSTOM_API_KEY}}"
    base_url: "https://my-api.example.com/v1"
```
## System Prompt

The `system_prompt` field defines the agent's behavior and instructions. It supports template expressions.

```yaml
agents:
  - id: assistant
    system_prompt: |
      You are a helpful coding assistant.
      Workspace: {{workspace}}

      You have access to tools via a discovery system.
      Use list_categories to see available modules,
      then browse_category and execute_tool to use them.
```
> **Note**: The system prompt is automatically enriched by the runtime with:
> - Agent identity header
> - Tool discovery instructions (native mode) or full tool schemas (text-based mode)
>
> Your system prompt is appended after these sections.

### Best Practices

1. **Be specific** — Define the agent's role, capabilities, and constraints
2. **Use variables** — Inject dynamic context with `{{variable_name}}`
3. **Guide tool usage** — Explain the workflow (list, browse, execute)
4. **Set limits** — "Limit yourself to 3-5 tool calls per question"
5. **Handle errors** — "If a tool fails, explain the error instead of retrying in a loop"

## Context Configuration (Per-Brain)

Each brain can override the execution-level context management:

```yaml
brain:
  provider: ollama
  model: mistral-nemo
  context:
    max_tokens: 8000         # Context window size (0 = auto-detect)
    output_reserved: 1000    # Tokens reserved for output
    strategy: truncate       # 'truncate' or 'summarize'
    keep_recent: 6           # Messages to keep during compaction
    compression_trigger: 0.60 # Compact at 60% usage
    summary_max_tokens: 512  # Max tokens for summary (summarize strategy)
    auto_compact: true       # Auto-inject compaction hook
    summary_brain:           # Optional: separate model for compaction
      provider: ollama
      model: qwen2.5:3b
      backend: openai_compat
```
The `summary_brain` field accepts the same fields as the main `brain` (`provider`, `model`, `backend`, `config`, `temperature`, `timeout`, etc.). If not set, the agent's main brain is used for summarization.

See [Context Management](06-context-management.md) for full details.

## Multi-Agent

Multiple agents can be defined in the `agents:` list:

```yaml
agents:
  - id: coordinator
    role: coordinator
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
    system_prompt: "You orchestrate tasks."

  - id: worker
    role: worker
    brain:
      provider: groq
      model: llama-3.3-70b-versatile
      backend: openai_compat
      config:
        api_key: "{{env.GROQ_API_KEY}}"
        base_url: "https://api.groq.com/openai/v1"
    system_prompt: "You execute tasks."
```
The `execution.entry_agent` field controls which agent starts. If not set, the first agent in the list is used.

## Complete Agent Example

This example shows all agent fields: brain with inline provider, generation params, per-brain context management with a separate summary brain, and a detailed system prompt.

```yaml
agents:
  - id: analyst
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      temperature: 0.2
      max_tokens: 8192
      top_p: 0.95
      timeout: 60.0
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
      context:
        max_tokens: 80000
        output_reserved: 4096
        strategy: summarize
        keep_recent: 10
        compression_trigger: 0.75
        summary_max_tokens: 1024
        auto_compact: true
        summary_brain:              # Cheap local model for compaction
          provider: ollama
          model: qwen2.5:3b
          backend: openai_compat
    system_prompt: |
      You are a data analyst assistant. You respond in French.
      You have access to tools via a discovery system.

      EFFICIENT WORKFLOW:
      1. list_categories -> see available modules
      2. browse_category(category="name") -> see module tools
      3. execute_tool(name="module.action", params={...}) -> execute

      IMPORTANT:
      - Go directly to execute_tool once you know the tool name.
      - Limit yourself to 3-5 tool calls per question.
      - If a tool fails, explain the error instead of retrying.
```