---

id: context-management
title: Context Management
sidebar_position: 7
format: md
---


# Context Management

LLM context windows are finite. When an agent has tools, memory, objectives, conversation history, and system prompts loaded, the context can explode. The context management system solves this.

## The Problem

A typical agent context includes:
- System prompt (~1000 tokens)
- Tool schemas (~3000 tokens for 20 tools)
- Cognitive state / objectives (~500 tokens)
- Project memory (~1000 tokens)
- Conversation history (grows unbounded)

Without management, the agent either runs out of context or loses awareness of critical state.

## Solution: Hybrid Approach

1. **Objectives are NEVER forgotten** — cognitive state (goals, progress, decisions) is always preserved at full fidelity
2. **Conversation history is compressible** — older messages are summarized by a fast LLM
3. **On-demand fetch** — when context was compressed, the agent can fetch full details about any topic
4. **Application-aware** — tool summaries respect the app's allowed modules/actions
5. **Token budget** — a global allocator distributes tokens across all context components

## Configuration

Context management is configured in the `loop.context:` block:

```yaml
agent:
  loop:
    context:
      # Basic context settings
      max_tokens: 200000                  # Total context window
      strategy: summarize                 # summarize | sliding_window | truncate
      keep_system_prompt: true
      keep_last_n_messages: 30
      summarize_older: true

      # Advanced budget management (context_manager module)
      model_context_window: 200000        # Total model context window
      output_reserved: 8192              # Reserved for model output
      cognitive_max_tokens: 1500          # Max for cognitive state
      memory_max_tokens: 2000            # Max for memory context
      compression_trigger_ratio: 0.75     # Compress at 75% usage
      summarization_model: ""            # Empty = use fast model (haiku)
      min_recent_messages: 10            # Always keep this many messages
```

### Context Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_tokens` | int | `200000` | Total context window size |
| `strategy` | enum | `summarize` | Context management strategy |
| `keep_system_prompt` | bool | `true` | Always keep system prompt |
| `keep_last_n_messages` | int | `30` | Recent messages to keep uncompressed |
| `summarize_older` | bool | `true` | Summarize older messages |
| `model_context_window` | int | `200000` | Total model context window in tokens |
| `output_reserved` | int | `8192` | Tokens reserved for output generation |
| `cognitive_max_tokens` | int | `1500` | Max tokens for cognitive state |
| `memory_max_tokens` | int | `2000` | Max tokens for memory context |
| `compression_trigger_ratio` | float | `0.75` | Compress when history uses this fraction |
| `summarization_model` | string | `""` | Model for summarization (empty = haiku) |
| `min_recent_messages` | int | `10` | Minimum recent messages to keep |
| `inject_on_start` | list[string] | `[]` | File paths to inject into context at run start |

## Token Budget Allocation

The context manager distributes the model's context window across components:

```
┌──────────────────────────────────────┐
│          Model Context Window        │
│              (200,000 tokens)        │
├──────────────────────────────────────┤
│  Output Reserved     │    8,192      │
├──────────────────────────────────────┤
│  System Prompt       │   ~1,200      │
│  Tool Schemas        │   ~3,500      │
│  Cognitive State     │   ~1,500      │
│  Memory Context      │   ~2,000      │
├──────────────────────────────────────┤
│  Conversation History│  ~183,608     │  ← Remaining budget
│  (compressible)      │               │
└──────────────────────────────────────┘
```

## Context Manager Tools

When the `context_manager` module is included in the agent's tools, the LLM can manage its own context:

```yaml
agent:
  tools:
    - module: context_manager
      action: get_budget
    - module: context_manager
      action: compress_history
    - module: context_manager
      action: fetch_context
    - module: context_manager
      action: get_tools_summary
    - module: context_manager
      action: get_state
```

### get_budget

Returns the current token allocation:

```json
{
  "model_context_window": 200000,
  "output_reserved": 8192,
  "budget_breakdown": {
    "tools": 3500,
    "system_prompt": 1200,
    "cognitive_state": 400,
    "memory": 800,
    "conversation_history": {
      "budget": 186108,
      "used": 45000
    }
  },
  "total_used": 50900,
  "utilization": "25.5%",
  "compression_needed": false
}
```

### compress_history

Manually compress older conversation messages:

```python
compress_history(keep_last_n=10)
```

Returns:
```json
{
  "compressed": true,
  "messages_before": 45,
  "messages_after": 11,
  "tokens_saved": 12000,
  "summary": "Previous conversation covered..."
}
```

### fetch_context

Retrieve full details from compressed segments:

```python
fetch_context(query="the authentication bug we discussed")
```

Returns:
```json
{
  "found": true,
  "content": "...",
  "segment_count": 3
}
```

### get_tools_summary

Get a compact summary of available tools (filtered by app permissions):

```python
get_tools_summary(module_filter="filesystem")
```

### get_state

Get the full context window state including compression history.

## Automatic Behavior

The runtime automatically manages context without the agent needing to call tools:

1. **Before each LLM call**, the runtime:
   - Updates the context manager's state (system prompt size, tool schemas, etc.)
   - Checks if compression is needed (`compression_trigger_ratio`)
   - Auto-compresses if needed
   - Bounds cognitive text (objectives preserved, other state truncated if needed)

2. **The agent can also manage context explicitly** by calling the context_manager tools when it needs to.

## System Prompt Example

Include context management awareness in the agent's system prompt:

```yaml
agent:
  system_prompt: |
    ## Context Management
    Your context window is managed automatically. The runtime compresses
    older messages and bounds cognitive injection to stay within budget.

    If you notice you're losing context or need details from earlier:
    - context_manager.get_budget() — see token allocation
    - context_manager.compress_history() — manually compress
    - context_manager.fetch_context(query) — retrieve compressed details
```
