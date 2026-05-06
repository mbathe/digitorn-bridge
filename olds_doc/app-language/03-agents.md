---
id: agents
---

# Agents

Each entry under the top-level `agents:` list is an `AgentDefinition`
(`packages/digitorn/core/app/schema.py:2170`, `extra: forbid`). An
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

`schema.py:2170`. Only `id` and `brain` are required; everything else
has a default.

| Field | Type | Default | Source |
|-------|------|---------|--------|
| `id` | string | *required* | `schema.py:2195`. Unique slug within the app. |
| `role` | string | `"worker"` | `schema.py:2196`. Functional roles drive runtime behavior: `coordinator` (can spawn agents), `specialist` (pre-configured expert), `worker`. Free-form descriptive roles (`assistant`, `analyst`, `reviewer`, ...) are also accepted and surfaced in the system prompt. |
| `brain` | AgentBrain | *required* | `schema.py:2205`. See [Brain configuration](#brain-configuration) below. |
| `system_prompt` | string | `""` | `schema.py:2206`. Injected at conversation start. Supports `{{...}}` templates. |
| `plan_first` | bool | `true` | `schema.py:2207`. When true, the agent must explain its plan in plain text before executing tools on the first turn. Prevents silent tool calls. |
| `specialty` | string | `""` | `schema.py:2214`. Short description of the specialist's expertise; shown to coordinators in the spawn catalog. |
| `delegate_to` | list[string] | `[]` | `schema.py:2218`. Agent IDs this coordinator can delegate to. The compiler verifies each entry references a declared agent id. |
| `skills` | string | `""` | `schema.py:2225`. Path to a `.md` file with detailed methodology / instructions for this specialist. |
| `capabilities` | list[string] | `[]` | `schema.py:2229`. Names of skill files to auto-load from the bundle's `skills/` directory. The compiler reads `skills/<name>.md` and appends the content under an `## Available capabilities` section in the system prompt. |
| `modules` | list[string \| dict] | `[]` | `schema.py:2241`. Per-agent module restriction. See [Per-agent module access](#per-agent-module-access) below. Empty = inherits from the coordinator. |
| `pool` | AgentPoolConfig | default-instance | `schema.py:2282`. See [Coordinator pool](#coordinator-pool). |
| `coordination` | CoordinationBlock\|None | `null` | `schema.py:2291`. Phase-9 grouped block (`delegate_to` + `pool`). Aliased into the legacy fields at compile. |
| `instructions` | InstructionsBlock\|None | `null` | `schema.py:2298`. Phase-9 grouped block (`file` + `capabilities` + `specialty`). Aliased into the legacy fields at compile. |
| `hooks` | list[HookConfig] | `[]` | `schema.py:2305`. Per-agent hooks merged with `runtime.hooks` but only evaluated when this agent is active. Use for specialist-specific behavior. App-wide hooks still fire for every agent; these add on top. |

## Brain configuration

`schema.py:830` `AgentBrain` (`extra: forbid`). Two declaration modes.

### Inline mode (recommended)

Embed the full provider config in the agent block.

```yaml
brain:
  provider: deepseek                  # Provider hint, validated against a known set
  model: deepseek-chat                # Model identifier
  backend: openai_compat              # 'openai_compat' (default) or 'anthropic'
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

| Field | Type | Default | Source |
|-------|------|---------|--------|
| `provider_id` | string\|None | `null` | `schema.py:854`. Reference to a named provider. If set, `provider`/`model`/`config` are ignored. |
| `provider` | string\|None | `null` | `schema.py:863`. Provider hint, validated against the known set. |
| `model` | string\|None | `null` | `schema.py:893`. Model identifier (e.g. `deepseek-chat`, `claude-sonnet-4-20250514`). |
| `backend` | `openai_compat | anthropic` | `openai_compat` | `schema.py:897`. Wire protocol. |
| `config` | dict | `{}` | `schema.py:901`. Provider-specific config (api_key, base_url, ...). |
| `credential` | string \| dict \| null | `null` | `schema.py:906`. Recommended over inline `{{secret.X}}`. Compact: `credential: openai_main`. Explicit: `credential: { ref: openai_main, scope: per_user }`. The runtime resolves at activation and injects into `config`. |
| `temperature` | float\|None | `null` | `schema.py:934` |
| `max_tokens` | int\|None | `null` | `schema.py:935` |
| `top_p` | float\|None | `null` | `schema.py:936` |
| `timeout` | float\|None | `null` | `schema.py:937`. Seconds. |
| `native_tool_use` | bool\|None | `null` (auto-detect) | `schema.py:939`. Override native tool-calling detection. See [Native vs text-based tool calling](#native-vs-text-based-tool-calling). |
| `context` | ContextConfig\|None | `null` | `schema.py:949`. Per-brain context override. Inherits from `runtime.context` if not set. |
| `fallback` | AgentBrain\|None | `null` | `schema.py:958`. Used when the primary returns a billing / credit error (HTTP 402, "Insufficient Balance"). Switches back on the next turn. |
| `vision` | bool\|None | `null` (auto) | `schema.py:973`. Whether the model supports image input. `null` = auto-detect from model name; `true`/`false` to force. |
| `image_generation` | bool | `false` | `schema.py:981` |
| `image_detail` | string | `"auto"` | `schema.py:989`. `auto` / `low` (512 px, cheaper) / `high` (native, more accurate). |
| `max_images_per_turn` | int [0, 100] | `5` | `schema.py:997`. `0` = unlimited. |

### Validated provider hints

`schema.py:878-892`. The `provider` field is validated against this
exact set (17 entries); any other value raises a compile error with a
"Did you mean..." suggestion.

```
anthropic, openai, deepseek, groq, mistral, together,
ollama, lm_studio, vllm,
google-gemini, gemini, xai, grok,
cerebras, perplexity, fireworks, github_copilot
```

A `custom` provider hint is **not** in this set — when targeting a
non-listed endpoint, pick the closest hint (usually `openai` or
`deepseek`) and override `config.base_url`.

### Native vs text-based tool calling

The framework detects which tool-calling format a provider supports.

- **Native** — tools are passed via the API `tools=` parameter; the
  LLM emits structured `tool_calls`. Default for: OpenAI, Anthropic,
  DeepSeek, Groq, Mistral, Together, xAI / Grok, Cerebras,
  Perplexity, Fireworks, Gemini.
- **Text-based** — tool schemas are injected into the system prompt;
  tool calls are parsed from the text output. Default for: Ollama,
  LM Studio, vLLM.

To override, set `native_tool_use: true` (force native) or
`native_tool_use: false` (force text). Useful for local models that
support native tool calling even though their provider defaults to
text-based (e.g. `qwen2.5-coder` on Ollama).

| `native_tool_use` | Behavior |
|-------------------|----------|
| `true` | Force native — tools via API `tools=` |
| `false` | Force text-based — tools in system prompt |
| `null` (default) | Auto-detect from provider hint |

#### Tool-call recovery

Even with native tool calling, models occasionally emit malformed or
text-wrapped tool calls. The framework's recovery parser (in the
provider streaming layer) handles:

1. Llama-style `<function=name{...}</function>` — regex parse
2. XML wrappers `<tool_call>{...}</tool_call>` — regex parse
3. Raw JSON in text — brace-matched extraction
4. Markdown code blocks — extracted from ` ```json ... ``` `
5. Smart quotes (`""`, `''`) — normalised to ASCII before parsing
6. Groq `tool_use_failed` errors — recovered from `failed_generation`

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
  model: claude-sonnet-4-20250514
  backend: anthropic
  config:
    api_key: "{{env.ANTHROPIC_API_KEY}}"

# Anthropic Claude Code OAuth
brain:
  provider: anthropic
  model: claude-sonnet-4-20250514
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

`schema.py:958`. When the primary returns a billing/credit error
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

See [credentials.md](../credentials.md) for the vault, scopes, OAuth
flows, and audit log.

## Per-agent module access

`schema.py:2241`. The `modules` field restricts which modules a
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

Validation is enforced server-side
(`schema.py:_validate_modules_shape:2253`):

- Every list entry is either a string (full module access) or a
  single-key dict mapping a module id to a list of action names.
- Granular dicts must have exactly one key — multi-key dicts raise
  a clear error pointing at the bad entry.
- Action lists must be `list[str]`.

The compiler builds a per-agent action filter from this list and
hands it to the context builder, so each specialist's tool index is
restricted at the schema level — the LLM never sees actions it
isn't allowed to call.

## Coordinator pool

`typed_models.py:197` `AgentPoolConfig`. Controls fan-out for an
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

`typed_models.py:111` `CoordinationBlock`.

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

`typed_models.py:130` `InstructionsBlock`.

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

`schema.py:2206`. The `system_prompt` is injected at conversation
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

1. **Agent identity** — auto-generated from `id`, `role`, `specialty`.
2. **Tool delivery** — either the discovery instructions
   (`list_categories`, `browse_category`, ...) for the discovery /
   compact_direct injection mode, or the full tool schemas for direct
   mode and text-based tool calling.
3. **Skills / capabilities** — content of every file referenced via
   `capabilities: [skill_name]` (read from `skills/<name>.md`).

`plan_first: true` (the default) makes the agent emit a one-paragraph
plan before its first tool call. Set to `false` for headless workers
where explanation is unnecessary.

## Per-brain context configuration

`schema.py:759` `ContextConfig`. Each brain can override
`runtime.context`. Eight fields, full reference in
[App Configuration → runtime.context](02-app-config.md#runtimecontext--context-window-management).

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

`schema.py:2305`. Hooks declared under an agent fire **only when that
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
isolation, and shared modules (the 5 modules — `memory`, `web`,
`lsp`, `filesystem`, `shell` — share a single instance between
coordinator and specialists).

## Cross-references

- Block reference: [App Configuration](02-app-config.md)
- LLM provider module: [modules/reference/llm_provider.md](../modules/reference/llm_provider.md)
- Multi-agent orchestration: [Multi-Agent](12-multi-agent.md)
- Context window management: [Context Management](06-context-management.md)
- Hooks: [Tool Hooks](31-tool-hooks.md)
- Credentials: [credentials.md](../credentials.md)
