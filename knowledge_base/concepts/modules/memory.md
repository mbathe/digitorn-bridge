---
id: module-concept-memory
title: "memory module - overview"
type: module-concept
module: memory
isolation: shared
keywords: [memory, memory-module, task_create, task_update, set_goal, remember]
version: 1.0.0
---

# `memory` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `1.0.0`
- **Actions**: 4 visible, 0 internal

## Description (from class docstring)

Memory Module - cognitive memory system for Digitorn agents.

Provides 5 memory layers (all opt-in):
- **Working Memory**: goal, plan, todo-list, facts, entities (always in prompt)
- **Episodic Memory**: session summaries (persistent)
- **Semantic Memory**: facts + entity graph (vector + graph)
- **Procedural Memory**: learned patterns
- **Memory Runtime**: proactive injection, content cache, goal guardian

The memory is rendered as a single text block injected into the system
prompt by the context_builder. The agent sees everything at once -
no queries needed. Like opening your eyes and knowing.

> Class-level summary: Cognitive memory for Digitorn agents.

    Memory is scoped by app + session:
    - **Per-session**: working memory, todos, episodic, content cache
    - **Per-app** (shared): semantic facts, graph, procedural patterns

    The module maintains a dict of session stores. ``get_session_store()``
    returns the store for a given session (creating it on demand).
    The ``store`` property returns the current active session store
    (set by the agent loop via ``set_active_session()``).

## Configuration

Set under `modules.memory.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon. |
| `working_memory` | bool |  | `False` |  |
| `todo_list` | bool |  | `False` |  |
| `checkpoint` | bool |  | `False` |  |
| `episodic` | bool |  | `False` |  |
| `semantic` | dict[str, Any] \| bool |  | `{}` |  |
| `procedural` | bool |  | `False` |  |
| `runtime` | dict |  | `{}` |  |
| `limits` | dict |  | `{}` |  |
| `security` | dict |  | `{}` |  |
| `auto_remember` | bool |  | `False` |  |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `task_create` | `TaskCreate` |  | low | Create a task to track your progress. |
| `task_update` | `TaskUpdate` |  | low | Update a task's status. |
| `set_goal` | `MemorySetGoal` |  | low | Set the main goal for this session. Internal - use Remember for goals. |
| `remember` | `Remember` |  | low | Store a fact that survives context compaction. |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: memory
      actions: [task_create, task_update, set_goal, remember]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {memory: [task_create, task_update, set_goal, remember]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/memory-*.md`.
