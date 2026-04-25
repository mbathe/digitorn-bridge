---
id: context-management
---

# Context Management

LLM context windows are finite. When conversation history grows unbounded, the agent either runs out of context or loses awareness of recent messages. Digitorn's context management system solves this.

## The Problem

A typical agent context includes:

- System prompt + tool discovery instructions (~500-2000 tokens)
- Tool schemas (~500-1500 tokens for 5 meta-tools)
- Conversation history (grows unbounded)

Without management, the agent hits a context overflow error from the API.

## Solution: Automatic Compaction

Digitorn uses a hook-based system that monitors context pressure and automatically compacts the conversation history when it gets too large.

Two strategies are available:

- **truncate** — Drop oldest messages, keeping only recent ones (fast, no LLM call)
- **summarize** — Summarize older messages into a compact summary, then keep recent ones (slower, requires LLM call, preserves more context)

## Configuration

Context management is configured at two levels:

### Execution-Level (default for all agents)

```yaml
execution:
  context:
    max_tokens: 0              # 0 = auto-detect from provider
    output_reserved: 4096      # Tokens reserved for output generation
    strategy: summarize        # 'truncate' or 'summarize'
    keep_recent: 10            # Recent messages to keep during compaction
    compression_trigger: 0.75  # Compact at 75% context usage
    summary_max_tokens: 1024   # Max tokens for summary (summarize only)
    auto_compact: true         # Auto-inject compaction hook
```
### Per-Brain Override (multi-agent or specific models)

```yaml
agents:
  - id: assistant
    brain:
      provider: ollama
      model: qwen2.5:14b
      context:
        max_tokens: 8000        # Small local model
        output_reserved: 1000
        strategy: truncate      # Fast — no LLM call needed
        keep_recent: 6
        compression_trigger: 0.60
        auto_compact: true
```
The per-brain config overrides the execution-level config for that specific agent.

### Context Config Fields

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `max_tokens` | int | `0` | Context window size in tokens. `0` = auto-detect from provider |
| `output_reserved` | int | `4096` | Tokens reserved for output generation |
| `strategy` | string | `"summarize"` | Compaction strategy: `truncate` or `summarize` |
| `keep_recent` | int | `10` | Number of recent messages to preserve during compaction |
| `compression_trigger` | float | `0.75` | Token pressure ratio (0.0-1.0) that triggers compaction |
| `summary_max_tokens` | int | `1024` | Maximum tokens for the summary (summarize strategy only) |
| `auto_compact` | bool | `true` | Automatically inject a compaction hook if none declared |
| `summary_brain` | AgentBrain | `null` | Optional separate brain for summarization (see [Summary Brain](#summary-brain-separate-model-for-compaction)) |

## How Auto-Compact Works

When `auto_compact: true` (the default), the bootstrap process automatically injects a `compact_context` hook if you haven't declared one yourself. This hook:

1. Fires at `turn_start` (before each LLM call)
2. Checks if context pressure exceeds `compression_trigger`
3. If so, compacts the conversation using the configured `strategy`

The pressure is calculated as:

```
pressure = estimated_tokens / (max_tokens - output_reserved)
```

Where `estimated_tokens` is a quick estimate (~4 chars per token) of all messages.

## Compaction Strategies

### Truncate

Fast, no LLM call. Simply drops old messages and keeps the most recent ones.

```
Before: [system, msg1, msg2, msg3, msg4, msg5, msg6, msg7, msg8]
After:  [system, "[Earlier messages truncated]", msg6, msg7, msg8]
```

Best for:

- Local models with small context windows
- Situations where latency matters
- Models that don't handle summaries well

### Summarize

Uses the LLM to create a summary of older messages, then keeps recent ones.

```
Before: [system, msg1, msg2, msg3, msg4, msg5, msg6, msg7, msg8]
After:  [system, "[Summary: discussed X, decided Y, found Z]", msg6, msg7, msg8]
```

Best for:

- Cloud models with large context windows
- Long conversations where context matters
- When you need to preserve decision history

## Context Reminder After Compaction

When context is compacted (truncate or summarize), the LLM loses awareness of its tools and what it has accomplished. To prevent this, a **context reminder** is automatically re-injected after compaction.

The reminder adapts to the tool injection mode:

- **Direct mode** (small toolsets): lists all available tools inline
- **Discovery mode** (large toolsets): shows categories + meta-tool instructions

This ensures the LLM retains its capabilities after compaction and doesn't hallucinate about which tools are available.

## Tool Result Truncation

When a tool returns a very large result (e.g., `filesystem.find` listing thousands of files), it can exceed the entire context window. The runtime automatically truncates oversized tool results:

- Each tool result is capped to **~50% of the available context**
- JSON arrays are truncated smartly: the first N items that fit are kept
- The LLM receives **explicit guidance** about the truncation:
  - How many results are shown vs the total
  - Suggestions to narrow the query (use a pattern, search by keyword)
  - An instruction not to guess or invent unseen results

Example of what the LLM sees after truncation:

```
[... first 200 file paths ...]

 RESULT TRUNCATED: showing 200 of 5000 results from filesystem.find.
The full result was too large for the context window.
To see more results, you can:
- Use a more specific pattern or filter (e.g. '*.py', 'src/**')
- Search for a specific filename or keyword instead of listing everything
- Ask the user to narrow their request
Do NOT guess or invent results you haven't seen. Only report what is shown above.
```

## Compaction Cooldown

After compaction runs, there is a **cooldown of 2 turns** before it can trigger again. This prevents infinite compaction loops that can occur when the system prompt + keep_recent messages alone exceed the compression threshold.

Without the cooldown, the following scenario would happen:

1. Turn N: pressure > trigger -- compact -- summarize (1 LLM call wasted)
2. Turn N+1: pressure still > trigger -- compact again -- summarize again
3. Repeat -- every turn wastes an LLM call on compaction -- timeout

The cooldown is persisted across turns in `AgentContext` (not the ephemeral `TurnState`).

## Summary Brain (Separate Model for Compaction)

By default, the `summarize` strategy uses the agent's main LLM to generate summaries. This can be expensive if the main model is a large cloud model. You can configure a **separate brain** for summarization:

```yaml
brain:
  provider: deepseek
  model: deepseek-chat
  backend: openai_compat
  config:
    api_key: "{{env.DEEPSEEK_API_KEY}}"
  context:
    strategy: summarize
    summary_brain:
      provider: ollama
      model: qwen2.5:3b
      backend: openai_compat
```
The `summary_brain` accepts the same fields as the main `brain` (`provider`, `model`, `backend`, `config`, `temperature`, `timeout`, etc.). If not configured, the agent's main brain is used as before.

This is useful for:
- Using a **fast/cheap model** for summaries (e.g., a small local model)
- Avoiding extra costs on expensive cloud APIs
- Faster compaction (smaller models respond quicker)

`summary_brain` can also be set at the execution level:

```yaml
execution:
  context:
    strategy: summarize
    summary_brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
```
## Emergency Compaction

If the LLM returns a context overflow error (HTTP 400 with "maximum context length" or similar), the agent loop triggers **emergency compaction**:

1. Aggressively reduces context to ~50% of max
2. Uses `keep_recent // 2` (more aggressive than normal)
3. Always uses truncate (no LLM call — the LLM is refusing requests)
4. Also truncates any oversized individual messages that remain
5. Re-injects a context reminder so the LLM retains tool awareness
6. Retries the LLM call once after compaction

This handles cases where the pressure estimate was wrong or where individual messages are very large.

## Hooks

Hooks are the mechanism that powers context management. They're condition-action pairs evaluated during the agent loop.

### Auto-Injected Hook

When `auto_compact: true`, this hook is injected automatically:

```yaml
# This is what auto_compact generates internally:
hooks:
  - id: _auto_compact
    on: turn_start
    condition:
      type: context_pressure
      threshold: 0.75           # From compression_trigger
    action:
      type: compact_context
      strategy: summarize       # From strategy
      keep_recent: 10           # From keep_recent
      summary_max_tokens: 1024  # From summary_max_tokens
    cooldown: 30.0
```
### Custom Hooks

You can declare your own hooks in `execution.hooks:`:

```yaml
execution:
  hooks:
    # Log context pressure every turn
    - id: pressure_log
      on: turn_start
      condition:
        type: always
      action:
        type: log
        message: "Turn {turn}: ~{tokens} tokens, {messages} messages"
      cooldown: 0

    # Custom compaction with aggressive settings
    - id: aggressive_compact
      on: turn_end
      condition:
        type: context_pressure
        threshold: 0.60
      action:
        type: compact_context
        strategy: truncate
        keep_recent: 4
      cooldown: 60
```
### Hook Fields

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `id` | string | *required* | Unique hook identifier |
| `on` | string | `"turn_end"` | When to evaluate: `turn_start` or `turn_end` |
| `condition` | object | *required* | Condition that must be true to fire |
| `action` | object | *required* | Action to execute when condition is met |
| `cooldown` | float | `0.0` | Minimum seconds between fires (prevents rapid re-firing) |

### Condition Types

| Type | Params | Description |
| ---- | ------ | ----------- |
| `context_pressure` | `threshold` (float, 0-1) | Fires when token usage exceeds threshold |
| `turn_count` | `count` (int) | Fires at a specific turn number |
| `tool_calls` | `threshold` (int) | Fires when tool call count exceeds threshold |
| `message_count` | `threshold` (int) | Fires when message count exceeds threshold |
| `always` | (none) | Fires every evaluation (use with cooldown) |

### Action Types

| Type | Params | Description |
| ---- | ------ | ----------- |
| `compact_context` | `strategy`, `keep_recent`, `summary_max_tokens` | Compact conversation history |
| `inject_message` | `message`, `role` | Inject a message into the conversation |
| `module_action` | `module`, `action`, `params` | Call any module action |
| `log` | `message` | Log a message (supports `{turn}`, `{tokens}`, `{messages}` placeholders) |

## Provider Auto-Detection

When `max_tokens: 0`, the runtime queries the provider for its context window size. Known context windows:

| Model | Context Window |
| ----- | -------------- |
| `deepseek-chat` | 131,072 |
| `gpt-4o` | 128,000 |
| `gpt-4o-mini` | 128,000 |
| `o3` / `o3-mini` | 200,000 |
| `llama-3.3-70b-versatile` | 128,000 |
| `mistral-large-latest` | 128,000 |

For unknown models (e.g., Ollama), set `max_tokens` explicitly in your YAML.

## Safe Split Points

During compaction, the system never breaks tool call sequences. If message N is an assistant message with `tool_calls`, and message N+1 is the tool result, they're always kept together. The `_find_safe_split_point()` function ensures this.

## Complete Example

This example demonstrates all context management features: per-brain context config with a separate summary brain, execution-level context defaults, a custom logging hook, and an aggressive compaction trigger for testing.

```yaml
app:
  app_id: context-test
  name: "Context Management Test"

modules:
  filesystem:
    constraints:
      allowed_actions: [read, glob, grep]

agents:
  - id: assistant
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
      context:
        max_tokens: 0                 # Auto-detect (131k for deepseek-chat)
        output_reserved: 4096         # Reserve for output generation
        strategy: summarize           # Use LLM to summarize old messages
        keep_recent: 6                # Keep last 6 messages after compaction
        compression_trigger: 0.15     # Very low — compaction after a few exchanges
        summary_max_tokens: 512       # Max summary length
        auto_compact: true            # Auto-inject compaction hook
        summary_brain:                # Use a cheap local model for summaries
          provider: ollama
          model: qwen2.5:3b
          backend: openai_compat
    system_prompt: |
      You are a test assistant. Be detailed in your responses.

execution:
  mode: conversation
  greeting: "Context management test. Each exchange fills the context."
  max_turns: 50
  timeout: 120.0
  context:                            # Execution-level defaults (overridden per-brain above)
    max_tokens: 0
    strategy: truncate                # Default strategy for agents without brain.context
    keep_recent: 10
    compression_trigger: 0.75
  hooks:
    # Log context pressure every turn
    - id: pressure_log
      on: turn_start
      condition:
        type: always
      action:
        type: log
        message: "Turn {turn}: ~{tokens} tokens, {messages} messages"
      cooldown: 0

    # Inject a reminder at turn 5
    - id: turn5_reminder
      on: turn_start
      condition:
        type: turn_count
        count: 5
      action:
        type: inject_message
        role: system
        message: "Reminder: you have been chatting for 5 turns."
      cooldown: 0

capabilities:
  default_policy: auto
```
### What happens at runtime

1. **Bootstrap**: Auto-detects deepseek-chat context window (131k). Per-brain `context` overrides execution-level defaults. `auto_compact: true` injects a compaction hook.
2. **Turn 1-2**: Normal conversation. Pressure log shows ~2000 tokens.
3. **Turn 3+**: With `compression_trigger: 0.15`, compaction fires once pressure exceeds 15%. The `summarize` strategy uses the local Ollama model (qwen2.5:3b) to create a summary.
4. **After compaction**: A context reminder is injected so the LLM retains tool awareness. A 2-turn cooldown prevents re-compaction.
5. **Turn 5**: The `turn5_reminder` hook fires, injecting a system message.
6. **If overflow**: Emergency compaction aggressively truncates + caps oversized messages.
