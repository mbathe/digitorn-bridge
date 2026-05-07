---
id: memory
title: memory Module
sidebar_label: memory
sidebar_position: 4
description: Cognitive memory - 4 LLM-exposed actions (TaskCreate, TaskUpdate, memory.set_goal, Remember) over 5 internal layers.
---

# memory

Cognitive memory for Digitorn agents. The module maintains
**5 internal memory layers** but exposes only **4 actions**
to the LLM. Memory content is rendered as a single text block
that `context_builder` injects into the system prompt every
turn - the agent reads everything at once, no queries.

| Property | Value |
|----------|-------|
| Module id | `memory` |
| Version | `1.0.0` |
| Action count | 4 (LLM-exposed) |
| Type | shared (per-app), per-session working state |
| Pip deps | None (KV backend optional: in-memory / SQLite / Redis). |

> **Older docs claimed ~16 memory actions** (set_plan,
> add_todo, recall, forget, ...). The codebase has **only
> 4** decorated with `@action`. The other entry points
> referenced in legacy docs do not exist.

## Memory layers (internal)

| Layer | Scope | Stored | Rendered |
|-------|-------|--------|----------|
| **Working memory** | per-session | goal, todos, facts, entities | always in system prompt. |
| **Episodic** | per-session | session summaries | loaded on resume. |
| **Semantic** | per-app (shared) | facts + entity graph | vector / graph retrieval (when `semantic_rag_enabled`). |
| **Procedural** | per-app | learned patterns | RAG retrieval. |
| **Memory runtime** | per-session | proactive injection + goal guardian | auto-injected pre-turn. |

## The 4 LLM-exposed actions

`module.py`. All marked silent in
 - they don't render in
the chat stream; the sidebar panel shows live goal / todos /
facts.

| Tool | FQN | Source | Visible params | Purpose |
|------|-----|--------|----------------|---------|
| `TaskCreate` | `memory.task_create` | | `subject`, `description?` | Create a task / todo for the agent's planning. |
| `TaskUpdate` | `memory.task_update` | | `taskId`, `status` | Update task status. |
| *(no short alias)* | `memory.set_goal` | | `goal` | Set the top-level session goal (typically called by the coordinator). Called via FQN. |
| `Remember` | `memory.remember` | | `content` | Store a fact that survives context compaction. |

### `Remember` - `content`

Adds a fact to working memory. Rendered in the system prompt
on every subsequent turn.

```
Remember(content="Test command: pytest tests/ -v")
Remember(content="Auth bug is in src/auth/validate.py:42")
```

**Redaction** (default `redact_secrets: true`): values from
env vars matching `key`, `secret`, `password`, `token`,
`auth`, `credential`, `private`, `jwt` are auto-scrubbed
before storage.

### `SetGoal` - `goal`

```
SetGoal(goal="Fix the authentication bug in src/auth/validate.py")
```

Appears at the top of the memory block. Usually called by the
coordinator (or via the `/goal` slash command); not exposed
to specialist sub-agents by default.

### `TaskCreate` - `subject` + optional `description`

Tasks are numbered (`t1`, `t2`, ...) and rendered in the
sidebar.

```
TaskCreate(subject="Find all call sites of OldApiClient")
TaskCreate(subject="Write migration tests",
           description="Cover INSERT, UPDATE, DELETE paths")
```

### `TaskUpdate` - `taskId` + `status`

| Status | Meaning |
|--------|---------|
| `pending` | Not started. |
| `in_progress` | Currently working on. |
| `completed` | Done. |
| `blocked` | Cannot proceed. |

```
TaskUpdate(taskId="t1", status="in_progress")
TaskUpdate(taskId="t1", status="completed")
```

## Configuration

```yaml
tools:
  modules:
    memory:
      config:
        max_facts: 50                       # per-session cap
        max_todos: 20
        redact_secrets: true                # scrub sensitive env values
        extra_sensitive_patterns: []        # additional regex patterns
        kv_backend: sqlite                  # null | sqlite | redis
        semantic_rag_enabled: false         # enable vector-based fact retrieval
```

## Session isolation

Working memory is keyed by the compound
`user_id::session_id` (`memory/module.py`). Two sessions
of the same user (or two users on the same shared session
app) never see each other's todos / facts / episodes.

## Memory rendering

`context_builder` injects memory into the system prompt
under `# Working Memory`:

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

Compaction hooks preserve this block verbatim when
summarising older turns, so the agent never loses its goal or
todos.

## Cross-references

- App-config block reference (`tools.modules.memory`):
  [App Configuration → tools.modules](../../language/02-app-config.md#toolsmodules---module-configuration)
- Cognitive memory deep-dive:
  [Cognitive Memory](../../language/05-memory.md)
- Sub-agents share the memory instance with the coordinator:
  [Agents → Sub-agent module sharing](../../language/03-agents.md)
