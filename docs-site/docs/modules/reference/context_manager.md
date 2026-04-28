---

id: context_manager
title: Context Manager Module
sidebar_position: 20
format: md
---




# context_manager

Intelligent LLM context window management. Computes token budgets, compresses conversation history via LLM summarization, provides on-demand context fetching, and generates compact tool summaries filtered by application permissions. Objectives are never forgotten - cognitive state is always preserved at full fidelity.

| Property | Value |
|----------|-------|
| **Module ID** | `context_manager` |
| **Version** | `1.0.0` |
| **Type** | system (daemon) |
| **Platforms** | All |
| **Dependencies** | None (optional: `tiktoken` for accurate token counting) |
| **Declared Permissions** | `context.read`, `context.write` |

---


## Actions

### get_budget

Get the current context budget allocation: how tokens are distributed across system prompt, cognitive state, memory, conversation history, and tools.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| *(none)* | - | - | - | This action takes no parameters |

**Returns**: `{"model_context_window": int, "output_reserved": int, "budget_breakdown": {"tools": int, "system_prompt": int, "cognitive_state": int, "memory": int, "conversation_history": {"budget": int, "used": int}}, "total_used": int, "utilization": "75.0%", "compression_needed": bool}`

**Security**: `context.read` (risk: low)

**IML Example**:
```json
{
  "id": "check-budget",
  "action": "get_budget",
  "module": "context_manager",
  "params": {}
}
```

---


### compress_history

Compress conversation history by summarizing older messages. Keeps the most recent messages intact. Uses LLM summarization when a summarizer is configured, otherwise falls back to extractive summarization.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `keep_last_n` | integer | No | `10` | Number of recent messages to keep uncompressed (1-200) |

**Returns**: `{"compressed": bool, "messages_before": int, "messages_after": int, "tokens_saved": int, "summary": "..."}`

**Security**: `context.write` (risk: low)

**IML Example**:
```json
{
  "id": "compress",
  "action": "compress_history",
  "module": "context_manager",
  "params": {
    "keep_last_n": 15
  }
}
```

---


### fetch_context

Fetch detailed context from compressed conversation segments. When older messages were compressed, use this to retrieve the full details about a specific topic or decision. Supports querying by keyword or retrieving a specific segment by index.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | Yes | - | What to look for in compressed history |
| `segment_index` | integer | No | `null` | Specific compression segment to retrieve (0 = most recent) |

**Returns**: `{"found": bool, "content": "...", "segment_count": int}`

**Security**: `context.read` (risk: low)

**IML Example**:
```json
{
  "id": "recall-decision",
  "action": "fetch_context",
  "module": "context_manager",
  "params": {
    "query": "database migration decision"
  }
}
```

---


### get_tools_summary

Get a compact summary of all available tools/actions. Filtered by application permissions (modules and actions the current application is allowed to use). More compact than full tool schemas - use when you need to check available capabilities.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `module_filter` | string | No | `""` | Only show tools from this module |

**Returns**: `{"summary": "...", "module_count": int, "action_count": int}`

**Security**: `context.read` (risk: low)

**IML Example**:
```json
{
  "id": "list-tools",
  "action": "get_tools_summary",
  "module": "context_manager",
  "params": {
    "module_filter": "filesystem"
  }
}
```

---


### get_state

Get the current context window state: token usage, budget utilization, compression history, and available compressed segments.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| *(none)* | - | - | - | This action takes no parameters |

**Returns**: `{"budget": {...}, "compressions_total": int, "compressions_recent": [{"timestamp": float, "messages_compressed": int, "tokens_before": int, "tokens_after": int, "ratio": "45.0%"}], "total_tokens_saved": int, "compressed_segments_available": int}`

**Security**: `context.read` (risk: low)

**IML Example**:
```json
{
  "id": "check-state",
  "action": "get_state",
  "module": "context_manager",
  "params": {}
}
```

---


## Design Principles

### Objectives Are Never Forgotten

The context manager enforces a hard invariant: **cognitive state (objectives, active goals) is never truncated**. When the cognitive text exceeds its budget, `bound_cognitive_text()` compresses the active context and recent decisions sections but always preserves the full objective text. If the objective itself exceeds the budget, it is kept anyway.

### Hybrid Compression

Rather than silently dropping old messages, the module uses a two-phase approach:

1. **Compress** - Older messages are summarized (via LLM or extractive fallback) and replaced with a compact summary message.
2. **On-demand fetch** - The full text of compressed segments is stored and retrievable via `fetch_context`, so the LLM can recall details when needed.

### Application Identity Integration

Tool summaries generated by `get_tools_summary` and `get_compact_tools_summary()` respect the application's `allowed_modules` and `allowed_actions` constraints. Only tools the application is permitted to use appear in the output.

### Token Counting

Uses `tiktoken` (cl100k_base encoding) when available for accurate token counts. Falls back to a heuristic of ~3.5 characters per token for mixed code/prose content.

---


## Configuration

The module is configured via `ContextBudgetConfig`:

| Setting | Default | Description |
|---------|---------|-------------|
| `model_context_window` | `200000` | Total model context capacity (tokens) |
| `output_reserved` | `8192` | Tokens reserved for generation output |
| `cognitive_max_tokens` | `1500` | Maximum tokens for cognitive state (objectives never truncated below this) |
| `memory_max_tokens` | `2000` | Maximum tokens for KV/vector/file memory |
| `compression_trigger_ratio` | `0.75` | Compress when history exceeds 75% of its budget |
| `summarization_model` | `""` | Model for summarization (empty = use same model) |
| `min_recent_messages` | `10` | Always keep last N messages uncompressed |

---


## Implementation Notes

- No external dependencies required - `tiktoken` is optional for better accuracy
- All actions are async
- Compression history is capped at 50 records (ring buffer via `deque`)
- Compressed segments are stored in memory for the lifetime of the module instance
- The extractive fallback keeps the last 20 items when LLM summarization is unavailable
- The `update_state()` method is called by the runtime before each LLM call to keep budget computations current

---


## YAML App Language

```yaml
name: context-aware-assistant
description: An assistant that monitors and manages its own context window

steps:
  - id: check-budget
    action: get_budget
    module: context_manager

  - id: maybe-compress
    action: compress_history
    module: context_manager
    params:
      keep_last_n: 20
    depends_on:
      - check-budget
    when: "{{result.check-budget.compression_needed}} == true"

  - id: recall-prior-work
    action: fetch_context
    module: context_manager
    params:
      query: "file changes made earlier"

  - id: available-tools
    action: get_tools_summary
    module: context_manager
    params:
      module_filter: ""

  - id: full-state
    action: get_state
    module: context_manager
```
