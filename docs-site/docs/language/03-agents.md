---
id: agents
---

# Agents

Each entry under the top-level `agents:` list is an `AgentDefinition`
(strict schema, `extra: forbid`). An
agent is an LLM with a brain, a system prompt, a role, and a
restricted set of modules it can call.

## Minimal definition

```yaml
agents:
  - id: assistant            # Required. Slug, unique within this app.
    role: assistant          # Default: "worker".
    brain:                   # Required. AgentBrain (see below).
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
    system_prompt: |
      You are a helpful assistant.
      Workspace: {{workspace}}
```

## `AgentDefinition` fields

 Only `id` and `brain` are required; everything else
has a default.

| Field | Type | Default |
|-------|------|---------|
| `id` | string | *required* |
| `role` | string | `"worker"` |
| `brain` | AgentBrain | *required* |
| `system_prompt` | string | `""` |
| `plan_first` | bool | `true` |
| `specialty` | string | `""` |
| `delegate_to` | list[string] | `[]` |
| `skills` | string | `""` |
| `capabilities` | list[string] | `[]` |
| `modules` | list[string \| dict] | `[]` |
| `pool` | AgentPoolConfig | default-instance |
| `coordination` | CoordinationBlock\|None | `null` |
| `instructions` | InstructionsBlock\|None | `null` |
| `hooks` | list[HookConfig] | `[]` |

## Brain configuration

`AgentBrain` (`extra: forbid`). Two declaration modes.

### Inline mode (recommended)

Embed the full provider config in the agent block.

```yaml
brain:
  provider: deepseek                  # Provider hint, validated against a known set
  model: deepseek-chat                # Model identifier
  backend: openai_compat              # 'openai_compat' (default), 'anthropic', or 'github_copilot'
  config:                             # Provider-specific config
    api_key: "{{env.DEEPSEEK_API_KEY}}"
    base_url: "https://api.deepseek.com/v1"   # optional if provider hint resolves it
  temperature: 0.2                    # Sampling temperature
  max_tokens: 8192                    # Max tokens to generate
  top_p: 1.0                          # Nucleus sampling
  timeout: 120.0                      # Request timeout in seconds
  context:                            # Per-brain context override
    max_tokens: 131072
    strategy: summarize
```

### Reference mode

Point at a named provider declared under
`tools.modules.llm_provider.config.providers`. When `provider_id` is
set, `provider`, `model`, and `config` on the brain are ignored.

```yaml
tools:
  modules:
    llm_provider:
      config:
        providers:
          deepseek_main:
            backend: openai_compat
            api_key: "{{env.DEEPSEEK_API_KEY}}"
            base_url: "https://api.deepseek.com/v1"
            model: deepseek-chat

agents:
  - id: assistant
    brain:
      provider_id: deepseek_main
      temperature: 0.2
```

### `AgentBrain` fields

| Field | Type | Default |
|-------|------|---------|
| `provider_id` | string\|None | `null` |
| `provider` | string\|None | `null` |
| `model` | string\|None | `null` |
| `backend` | `openai_compat` \| `anthropic` \| `github_copilot` | `openai_compat` |
| `config` | dict | `{}` |
| `credential` | string \| dict \| null | `null` |
| `temperature` | float\|None | `null` |
| `max_tokens` | int\|None | `null` |
| `top_p` | float\|None | `null` |
| `timeout` | float\|None | `null` |
| `native_tool_use` | bool\|None | `null` (auto-detect) |
| `context` | ContextConfig\|None | `null` |
| `fallback` | AgentBrain\|None | `null` |
| `vision` | bool\|None | `null` (auto) |
| `image_generation` | bool | `false` |
| `image_detail` | string | `"auto"` |
| `max_images_per_turn` | int [0, 100] | `5` |

### Known provider hints {#validated-provider-hints}

The `provider` field is a free-form string used to look up a
default `base_url` and pick a tool-call format. The runtime
recognises these names:

```
anthropic, openai, deepseek, groq, mistral, together,
ollama, lm_studio, vllm,
google-gemini, gemini, xai, grok,
cerebras, perplexity, fireworks, github_copilot
```

For an endpoint not in this list, pick the closest hint
(usually `openai` or `deepseek`) and override `config.base_url`
explicitly. Unknown hints are accepted at compile time and fall
back to OpenAI-compatible defaults.

### Native vs text-based tool calling

The framework detects which tool-calling format a provider supports.

- **Native** - tools are passed via the API `tools=` parameter; the
  LLM emits structured `tool_calls`. Default for: OpenAI, Anthropic,
  DeepSeek, Groq, Mistral, Together, xAI / Grok, Cerebras,
  Perplexity, Fireworks, Gemini.
- **Text-based** - tool schemas are injected into the system prompt;
  tool calls are parsed from the text output. Default for: Ollama,
  LM Studio, vLLM.

To override, set `native_tool_use: true` (force native) or
`native_tool_use: false` (force text). Useful for local models that
support native tool calling even though their provider defaults to
text-based (e.g. `qwen2.5-coder` on Ollama).

| `native_tool_use` | Behavior |
|-------------------|----------|
| `true` | Force native - tools via API `tools=` |
| `false` | Force text-based - tools in system prompt |
| `null` (default) | Auto-detect from provider hint |

#### Tool-call recovery

Even with native tool calling, models occasionally emit malformed or
text-wrapped tool calls. The framework's recovery parser (in the
provider streaming layer) handles:

1. Llama-style `<function=name{...}</function>` - regex parse
2. XML wrappers `<tool_call>{...}</tool_call>` - regex parse
3. Raw JSON in text - brace-matched extraction
4. Markdown code blocks - extracted from````json ... ``` `
5. Smart quotes (`""`, `''`) - normalised to ASCII before parsing
6. Groq `tool_use_failed` errors - recovered from `failed_generation`

### Provider examples

```yaml
# DeepSeek (cloud, native tool use)
brain:
  provider: deepseek
  model: deepseek-chat
  backend: openai_compat
  config:
    api_key: "{{env.DEEPSEEK_API_KEY}}"

# Anthropic (native backend)
brain:
  provider: anthropic
  model: claude-sonnet-4-5
  backend: anthropic
  config:
    api_key: "{{env.ANTHROPIC_API_KEY}}"

# Anthropic Claude Code OAuth
brain:
  provider: anthropic
  model: claude-sonnet-4-5
  backend: anthropic
  config:
    api_key: "claude-code"          # alias - reads ~/.claude/.credentials.json

# Groq (cloud, fast inference)
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

# Ollama with native tool calling (model supports it)
brain:
  provider: ollama
  model: qwen2.5-coder:7b
  native_tool_use: true
  backend: openai_compat
  config:
    base_url: "http://localhost:11434/v1"

# Generic OpenAI-compatible endpoint
brain:
  provider: openai                    # closest known hint
  model: my-fine-tuned-model
  backend: openai_compat
  config:
    api_key: "{{env.CUSTOM_API_KEY}}"
    base_url: "https://my-api.example.com/v1"
```

### Fallback brain

 When the primary returns a billing/credit error
(HTTP 402, "Insufficient Balance"), the daemon transparently switches
to the fallback for that turn and reverts to the primary on the
next turn.

```yaml
brain:
  provider: deepseek
  model: deepseek-chat
  backend: openai_compat
  config:
    api_key: "{{secret.DEEPSEEK_API_KEY}}"
  fallback:
    provider: ollama
    model: qwen2.5:7b-instruct
    backend: openai_compat
    config:
      base_url: "http://localhost:11434/v1"
    temperature: 0.1
    max_tokens: 8192
```

`fallback` accepts every field of `AgentBrain` recursively.

### Credentials block (instead of inline secrets)

For production apps, prefer a credential reference over inline
`{{secret.X}}` templates. The reference is resolved at activation
time and the credential's fields are merged into `config`.

```yaml
# Compact form
brain:
  credential: openai_main

# Explicit form
brain:
  credential:
    ref: openai_main
    scope: per_user                   # system_wide | per_app_shared | per_user | per_app_per_user
    provider: openai                  # optional override of the catalog provider
```

See [credentials.md](../reference/runtime/credentials.md) for the vault, scopes, OAuth
flows, and audit log.

## Per-agent module access

 The `modules` field restricts which modules a
specialist can call. Empty (default) = the agent inherits the
coordinator's module set.

Two formats are supported (mix is OK):

```yaml
agents:
  - id: explorer
    role: specialist
    modules:
      - filesystem                    # full module access
      - { shell: [bash] }             # only the bash action on shell
      - { memory: [recall] }          # single action on memory

  - id: writer
    role: specialist
    modules:
      - { memory: [remember] }        # writer has only memory.remember
```

Validation is enforced server-side (`_validate_modules_shape`
in):

- Every list entry is either a string (full module access) or a
  single-key dict mapping a module id to a list of action names.
- Granular dicts must have exactly one key - multi-key dicts raise
  a clear error pointing at the bad entry.
- Action lists must be `list[str]`.

The compiler builds a per-agent action filter from this list and
hands it to the context builder, so each specialist's tool index is
restricted at the schema level - the LLM never sees actions it
isn't allowed to call.

## Coordinator pool

`AgentPoolConfig`. Controls fan-out for an
agent that can spawn specialists via the `agent_spawn` module.

```yaml
agents:
  - id: coordinator
    role: coordinator
    pool:
      max_workers: 5                  # default 3, ge=1, le=100
      progress: true                  # relay specialist progress events to coordinator
      auto_retry: 1                   # default 0, ge=0, le=5
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_workers` | int [1, 100] | `3` | Maximum concurrent specialists. |
| `progress` | bool | `false` | Relay progress events from spawned agents. |
| `auto_retry` | int [0, 5] | `0` | Automatic retries on specialist failure. |

## Phase-9 grouped sub-blocks

Two optional sub-blocks group related fields. Both are aliased into
the legacy flat fields at compile time, so picking one shape doesn't
break readers that look at the other.

### `coordination`

`CoordinationBlock`.

```yaml
agents:
- id: coordinator
  delegate_to:
  - explorer
  - writer
  pool:
    max_workers: 5
    progress: true
```

Equivalent to setting `delegate_to` and `pool` directly on the agent.
When the new shape is set, it wins; the legacy fields are populated
from it for backwards compatibility.

### `instructions`

`InstructionsBlock`.

```yaml
agents:
- id: reviewer
  skills: ./instructions/review.md
  capabilities:
  - git_review
  specialty: Adversarial code review
```

Equivalent to the historical scattered `skills` (file path),
`capabilities` (list of skill names), and `specialty` (string).

## System prompt

 The `system_prompt` is injected at conversation
start. It supports every template namespace from
[App Configuration → Variables](02-app-config.md#variables).

```yaml
agents:
  - id: assistant
    system_prompt: |
      You are {{app.name}} v{{app.version}}.
      Working directory: {{workspace}}
      Today: {{sys.date}}

      Use {{prompt.tool_usage_intro}} when you need to explain how
      tools work.
```

The runtime enriches the system prompt with three sections appended
in order:

1. **Agent identity** - auto-generated from `id`, `role`, `specialty`.
2. **Tool delivery** - either the discovery instructions
   (`list_categories`, `browse_category`, ...) for the discovery /
   compact_direct injection mode, or the full tool schemas for direct
   mode and text-based tool calling.
3. **Skills / capabilities** - content of every file referenced via
   `capabilities: [skill_name]` (read from `skills/<name>.md`).

`plan_first: true` (the default) makes the agent emit a one-paragraph
plan before its first tool call. Set to `false` for headless workers
where explanation is unnecessary.

## Per-brain context configuration

`ContextConfig`. Each brain can override
`runtime.context`. Eight fields, full reference in
[App Configuration → runtime.context](02-app-config.md#runtimecontext---context-window-management).

```yaml
brain:
  provider: ollama
  model: mistral-nemo
  context:
    max_tokens: 8000               # 0 = auto-detect from provider
    output_reserved: 1000
    strategy: truncate             # truncate | summarize
    keep_recent: 6
    compression_trigger: 0.60      # 0.0–1.0
    summary_max_tokens: 512
    auto_compact: true
    summary_brain:                 # optional cheap model for summaries
      provider: ollama
      model: qwen2.5:3b
      backend: openai_compat
```

`summary_brain` accepts the full `AgentBrain` shape recursively. If
not set, the agent's main brain is used for summarization. See
[Context Management](06-context-management.md) for the compaction
algorithm.

## Per-agent hooks

 Hooks declared under an agent fire **only when that
agent is active**. They merge with the app-wide `runtime.hooks` (which
fire for every agent).

```yaml
agents:
  - id: reviewer
    role: specialist
    hooks:
      - id: ruff_after_write
        "on": tool_end                 # quoted - YAML 1.1 parses bare `on` as True
        condition:
          type: tool_name
          match: filesystem.write
        action:
          type: shell
          command: "ruff check {{tool.params.path}}"

tools:
  modules:
    filesystem: {}
    shell: {}                          # required: action `shell` runs through shell.bash
  capabilities:
    default_policy: auto
```

The compiler enforces the dependency: a hook that uses `action: shell` will fail compile unless `shell` is declared under `tools.modules`. This prevents silent no-ops at runtime.

See [Tool Hooks](31-tool-hooks.md) for the full hook surface.

## Multi-agent

Multiple agents are declared as list entries. The starting agent is
controlled by `runtime.entry_agent`; if not set, the first agent in
the list is used.

```yaml
runtime:
  entry_agent: coordinator

agents:
  - id: coordinator
    role: coordinator
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
    delegate_to: [explorer, writer]
    pool:
      max_workers: 3
    system_prompt: "You orchestrate tasks."

  - id: explorer
    role: specialist
    specialty: "Read-only codebase exploration"
    modules:
      - { filesystem: [read, glob, grep] }
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"

  - id: writer
    role: specialist
    specialty: "Apply edits"
    modules:
      - filesystem
      - { shell: [bash] }
    brain:
      provider: groq
      model: llama-3.3-70b-versatile
      backend: openai_compat
      config:
        api_key: "{{env.GROQ_API_KEY}}"
        base_url: "https://api.groq.com/openai/v1"
```

See [Multi-Agent](12-multi-agent.md) for delegation patterns,
isolation, and shared modules (the 5 modules - `memory`, `web`,
`lsp`, `filesystem`, `shell` - share a single instance between
coordinator and specialists).

## Cross-references

- Block reference: [App Configuration](02-app-config.md)
- LLM provider module: [modules/reference/llm_provider.md](../reference/modules/llm_provider.md)
- Multi-agent orchestration: [Multi-Agent](12-multi-agent.md)
- Context window management: [Context Management](06-context-management.md)
- Hooks: [Tool Hooks](31-tool-hooks.md)
- Credentials: [credentials.md](../reference/runtime/credentials.md)
