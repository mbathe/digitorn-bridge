---

id: memory
title: Memory Module
sidebar_position: 21
format: md
---




# memory

Multi-backend memory system with cognitive persistence. Store, recall, and search across pluggable backends: kv (fast persistent), vector (semantic search), file (markdown), cognitive (objective-driven). Users can register custom backends at runtime.

| Property | Value |
|----------|-------|
| **Module ID** | `memory` |
| **Version** | `1.0.0` |
| **Type** | system |
| **Platforms** | All |
| **Dependencies** | `aiosqlite` (kv), `chromadb` (vector, optional), None (file, cognitive) |
| **Declared Permissions** | `data.memory.read`, `data.memory.write` |

---


## Actions

### store

Store a key-value pair in a memory backend.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `key` | string | Yes | - | Key to store under |
| `value` | string | Yes | - | Value to store |
| `backend` | string | No | `"kv"` | Backend to use (`kv`, `vector`, `file`, `cognitive`) |
| `metadata` | object | No | `null` | Optional metadata dict |
| `ttl_seconds` | number | No | `null` | Time-to-live in seconds (0 = forever) |

**Returns**: `{"stored": true, "key": "...", "value": "...", "backend": "kv"}`

**Security**: `data.memory.write` (risk: low)

**IML Example**:
```json
{
  "id": "store-user-pref",
  "action": "store",
  "module": "memory",
  "params": {
    "key": "user.preferred_language",
    "value": "python",
    "backend": "kv",
    "metadata": {"source": "user_input"},
    "ttl_seconds": 86400
  }
}
```

---


### recall

Recall a value by key from a memory backend.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `key` | string | Yes | - | Key to recall |
| `backend` | string | No | `"kv"` | Backend to query |

**Returns**: `{"found": true, "key": "...", "value": "...", "metadata": {...}, "backend": "kv"}`

**Security**: `data.memory.read` (risk: low)

**IML Example**:
```json
{
  "id": "recall-pref",
  "action": "recall",
  "module": "memory",
  "params": {
    "key": "user.preferred_language"
  }
}
```

---


### search

Semantic or fuzzy search across one or all memory backends.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | Yes | - | Search query |
| `backend` | string | No | `null` | Backend to search (omit to search all) |
| `top_k` | integer | No | `5` | Max results to return (1-100) |

**Returns**: `{"results": [{"key": "...", "value": "...", "score": 0.95, "backend": "vector", "metadata": {...}}], "count": 3}`

**Security**: `data.memory.read` (risk: low)

**IML Example**:
```json
{
  "id": "search-docs",
  "action": "search",
  "module": "memory",
  "params": {
    "query": "how to configure logging",
    "backend": "vector",
    "top_k": 10
  }
}
```

---


### delete

Delete a key from a memory backend.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `key` | string | Yes | - | Key to delete |
| `backend` | string | No | `"kv"` | Backend to delete from |

**Returns**: `{"deleted": true, "key": "...", "backend": "kv"}`

**Security**: `data.memory.write` (risk: medium)

**IML Example**:
```json
{
  "id": "delete-stale",
  "action": "delete",
  "module": "memory",
  "params": {
    "key": "temp.scratch_data",
    "backend": "kv"
  }
}
```

---


### list_keys

List stored keys in a backend with optional prefix filtering.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `backend` | string | No | `"kv"` | Backend to list keys from |
| `prefix` | string | No | `null` | Filter by key prefix |
| `limit` | integer | No | `100` | Max keys to return (1-10000) |

**Returns**: `{"keys": ["key1", "key2"], "count": 2, "backend": "kv"}`

**Security**: `data.memory.read` (risk: low)

**IML Example**:
```json
{
  "id": "list-user-keys",
  "action": "list_keys",
  "module": "memory",
  "params": {
    "backend": "kv",
    "prefix": "user.",
    "limit": 50
  }
}
```

---


### clear

Clear all entries from a backend.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `backend` | string | Yes | - | Backend to clear (`kv`, `vector`, `file`, `cognitive`) |

**Returns**: `{"cleared": 42, "backend": "kv"}`

**Security**: `data.memory.write` (risk: high)

**IML Example**:
```json
{
  "id": "clear-temp",
  "action": "clear",
  "module": "memory",
  "params": {
    "backend": "file"
  }
}
```

---


### list_backends

List all registered memory backends and their capabilities.

No parameters.

**Returns**: `{"backends": [{"id": "kv", "description": "...", "supports_search": true}], "count": 4, "default": "kv"}`

**Security**: `data.memory.read` (risk: low)

**IML Example**:
```json
{
  "id": "show-backends",
  "action": "list_backends",
  "module": "memory",
  "params": {}
}
```

---


### set_objective

Set a cognitive objective that stays in permanent memory until completed. The objective filters all subsequent actions and is auto-injected into LLM prompts when the cognitive backend is active.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `goal` | string | Yes | - | The primary objective/goal |
| `sub_goals` | array | No | `[]` | List of sub-goals to track |
| `success_criteria` | array | No | `[]` | Criteria for completion |

**Returns**: `{"objective": {"goal": "...", "sub_goals": [...], "progress": 0.0}}`

**Security**: `data.memory.write` (risk: low)

**IML Example**:
```json
{
  "id": "set-goal",
  "action": "set_objective",
  "module": "memory",
  "params": {
    "goal": "Refactor the authentication module",
    "sub_goals": [
      "Extract token validation",
      "Add refresh token support",
      "Write integration tests"
    ],
    "success_criteria": [
      "All tests pass",
      "No regressions in existing auth flow"
    ]
  }
}
```

---


### get_context

Get the current cognitive context including the active objective, active state, and recent decisions. Auto-injected into LLM prompts when the cognitive backend is active.

No parameters.

**Returns**: `{"objective": {...}, "active_context": {...}, "recent_decisions": [...]}`

**Security**: `data.memory.read` (risk: low)

**IML Example**:
```json
{
  "id": "check-context",
  "action": "get_context",
  "module": "memory",
  "params": {}
}
```

---


### update_progress

Update the progress of the current cognitive objective.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `progress` | number | Yes | - | Progress from 0.0 to 1.0 |
| `completed_sub_goal` | string | No | `null` | Name of sub-goal just completed |
| `complete` | boolean | No | `false` | Mark objective as fully completed |

**Returns**: `{"progress": 0.66, "completed": false, "objective": {...}}`

**Security**: `data.memory.write` (risk: low)

**IML Example**:
```json
{
  "id": "mark-progress",
  "action": "update_progress",
  "module": "memory",
  "params": {
    "progress": 0.66,
    "completed_sub_goal": "Extract token validation"
  }
}
```

---


### observe

Get a real-time snapshot of ALL memory state across ALL backends. Returns a human-readable summary so the LLM understands the full state without looking up specific keys. Use this at the start of a conversation or whenever full state awareness is needed.

No parameters.

**Returns**: `{"cognitive": {"has_objective": true, "goal": "...", "progress": "66%", ...}, "backends": {"kv": {"key_count": 5, "sample_keys": [...], "contents": {...}}, ...}, "summary": "Active objective: ... (66%). kv: 5 entries. file: 2 entries."}`

**Security**: `data.memory.read` (risk: low)

**IML Example**:
```json
{
  "id": "full-snapshot",
  "action": "observe",
  "module": "memory",
  "params": {}
}
```

---


## Backends

The memory module dispatches operations to pluggable backends. Four built-in backends are available:

| Backend | ID | Description | Supports Search |
|---------|----|-------------|-----------------|
| **KV** | `kv` | Fast persistent key-value store backed by SQLite | Prefix matching |
| **Vector** | `vector` | Semantic search via ChromaDB embeddings | Semantic (cosine similarity) |
| **File** | `file` | Markdown file-based storage for human-readable persistence | Fuzzy text matching |
| **Cognitive** | `cognitive` | Objective-driven memory with goal tracking and decision history | Context-aware |

Custom backends can be registered at runtime via `register_backend()` by implementing `BaseMemoryBackend`.

---


## Implementation Notes

- All backend I/O is async - backends implement `async def store()`, `recall()`, `search()`, etc.
- The default backend is `kv` unless changed via `set_default_backend()`
- The `observe` action reads up to 20 keys per backend and includes full contents for backends with 10 or fewer keys; values longer than 200 characters are truncated
- The cognitive backend auto-injects objective context into LLM prompts via `get_cognitive_prompt()`
- Backend lifecycle is managed by module lifecycle: `on_start()` initializes all backends, `on_stop()` closes them

---


## YAML App Language

```yaml
app:
  name: research-assistant
  description: Research assistant with persistent memory and cognitive objectives

memory:
  default_backend: kv
  backends:
    - kv
    - vector
    - cognitive

plans:
  - name: start-research
    description: Set a research objective and observe current state
    actions:
      - id: set-goal
        module: memory
        action: set_objective
        params:
          goal: "Research distributed consensus algorithms"
          sub_goals:
            - "Survey Raft and Paxos"
            - "Compare performance characteristics"
            - "Write summary report"

      - id: check-state
        module: memory
        action: observe
        depends_on: [set-goal]

  - name: store-finding
    description: Store a research finding in memory
    actions:
      - id: save
        module: memory
        action: store
        params:
          key: "findings.raft_overview"
          value: "Raft uses leader election with randomized timeouts..."
          backend: kv
          metadata:
            source: "paper"
            topic: "consensus"

      - id: index
        module: memory
        action: store
        params:
          key: "findings.raft_overview"
          value: "Raft uses leader election with randomized timeouts..."
          backend: vector

  - name: search-findings
    description: Search across all backends for relevant findings
    actions:
      - id: search
        module: memory
        action: search
        params:
          query: "leader election mechanisms"
          top_k: 5

      - id: progress
        module: memory
        action: update_progress
        depends_on: [search]
        params:
          progress: 0.33
          completed_sub_goal: "Survey Raft and Paxos"
```
