---
id: memory
title: Memory Module
sidebar_label: memory
sidebar_position: 4
description: Cognitive memory with 4 agent-facing actions - Remember, SetGoal, TaskCreate, TaskUpdate.
---

# memory

Cognitive memory system for Digitorn agents. The module maintains 5 memory layers, but only **4 actions** are exposed to the LLM.

Memory content is rendered as a single text block injected into the system prompt by `context_builder`. The agent reads everything at once - no queries needed.

| Property | Value |
|----------|-------|
| **Module ID** | `memory` |
| **Isolation** | shared across sessions (per-app), per-session working memory |
| **Platforms** | All |
| **Dependencies** | None (KV backend optional: in-memory / SQLite / Redis) |

---

## Memory Layers

| Layer | Scope | Stored | Rendered |
|-------|-------|--------|----------|
| **Working memory** | per-session | goal, todos, facts, entities | always in system prompt |
| **Episodic** | per-session | session summaries | loaded on resume |
| **Semantic** | per-app (shared) | facts + entity graph | vector/graph retrieval |
| **Procedural** | per-app | learned patterns | RAG retrieval |
| **Memory runtime** | per-session | proactive injection + goal guardian | auto-injected pre-turn |

---

## Actions (4)

| Tool Name | Action | Visible Params | Description |
|-----------|--------|----------------|-------------|
| `Remember` | `remember` | `content` | Store a fact that survives context compaction. |
| `SetGoal` | `set_goal` | `goal` | Set the main goal for this session. (Internal; use `Remember` from the LLM.) |
| `TaskCreate` | `task_create` | `subject`, `description?` | Create a task/todo for the agent's own planning. |
| `TaskUpdate` | `task_update` | `taskId`, `status` | Update task status (`pending`, `in_progress`, `completed`, `blocked`). |

These are **silent tools** - they don't show up in chat turn output (see `_SILENT_TOOLS` in `core/cli/tui/app.py`). The sidebar panel displays goal + todos + facts in real time.

---

### Remember - `content`

Store a piece of knowledge that will survive context compaction. The fact is added to working memory and rendered in the system prompt on every subsequent turn.

```
Remember(content="Test command: pytest tests/ -v")
Remember(content="Auth bug is in src/auth/validate.py:42")
```

**Redaction:** values from env vars matching `key`, `secret`, `password`, `token`, `auth`, `credential`, `private`, `jwt` are auto-redacted before storage (configurable via `redact_secrets` / `extra_sensitive_patterns` in module config).

---

### SetGoal - `goal`

Set the top-level goal for the current session. Appears at the top of the memory block.

Generally called internally (by the coordinator agent or via slash command `/goal`). Not exposed to specialist/sub-agents by default.

```
SetGoal(goal="Fix the authentication bug in src/auth/validate.py")
```

---

### TaskCreate - `subject` + optional `description`

Create a task (todo) the agent can check off as it works. Tasks are numbered (`t1`, `t2`, ...) and rendered in the sidebar.

```
TaskCreate(subject="Find all call sites of OldApiClient")
TaskCreate(subject="Write migration tests", description="Cover INSERT, UPDATE, DELETE paths")
```

---

### TaskUpdate - `taskId` + `status`

Update the status of an existing task.

| Status | Meaning |
|--------|---------|
| `pending` | Not started |
| `in_progress` | Currently working on |
| `completed` | Done |
| `blocked` | Cannot proceed |

```
TaskUpdate(taskId="t1", status="in_progress")
TaskUpdate(taskId="t1", status="completed")
```

---

## YAML configuration

```yaml
modules:
  memory:
    config:
      max_facts: 50                  # cap per-session facts
      max_todos: 20
      redact_secrets: true           # scrub sensitive env var values
      extra_sensitive_patterns: []   # additional regex patterns
      kv_backend: sqlite             # null | sqlite | redis
      semantic_rag_enabled: false    # enable vector-based fact retrieval
```
---

## Memory rendering

The `context_builder` module injects memory into the system prompt under `# Working Memory`:

```
# Working Memory
## Goal
Fix authentication bug in src/auth/validate.py

## Todos
- [x] t1: Find all call sites (completed)
- [ ] t2: Write migration tests (in_progress)

## Facts
- Test command: pytest tests/ -v
- Auth bug is in src/auth/validate.py:42
```

Compaction hooks preserve this block verbatim when summarizing older turns, so the agent never loses its goal or todos.
