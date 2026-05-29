---
id: memory
---

# Cognitive Memory

The `memory` module gives an agent a small
working brain: a goal, a todo list, persistent facts that survive
context compaction, and a per-session/per-user store the runtime
auto-injects into the system prompt.

Every action and behaviour on this page maps to real code; entries
are cited with file + line.

## What the agent sees vs what's stored

The memory layer exposes **4 actions** to the LLM and stores **5
layers** internally. The agent doesn't issue queries for memory -
the runtime renders the relevant pieces into the system prompt at
turn start and the agent simply reads them.

```yaml
tools:
  modules:
    memory:
      config:
        working_memory: true       # goal + todos rendered into prompt
        todo_list: true            # alias of working_memory.todo_list
        episodic: true             # per-session event log
        semantic: true             # facts + entity graph (per-user)
        procedural: true           # learned patterns (per-app)
        auto_remember: false       # passive fact extraction (off by default)
        security:
          redact_secrets: true     # default true; matches key/secret/token/auth/...
        runtime: {}                # proactive injection knobs (advanced)
        limits: {}                 # caps on todos, facts, episodes (advanced)
```

The memory module's config schema uses `extra: allow` so it
tolerates forward-compatible knobs.

## The 4 LLM-exposed actions

| Action | Short alias | What it does |
|--------|-------------|--------------|
| `memory.task_create` | `TaskCreate` | Create a task in the todo list. Surfaces in the dedicated client panel. |
| `memory.task_update` | `TaskUpdate` | Update a task's status. |
| `memory.set_goal` | - | Set or replace the session goal. |
| `memory.remember` | `Remember` | Store a fact that survives context compaction. |

The short aliases above are the names exposed to the LLM.
Anything else (`set_plan`, `update_plan_step`, `add_todo`,
`update_todo`,
`note`, `resolve_note`, `add_fact`, `recall`, `forget`,
`track_entity`, `add_relationship`, `checkpoint`, `cache_content`,
`get_snapshot`, `add_episode`, …) referenced in older docs **does
not exist**. The four actions above are the entire LLM-callable
surface.

### `memory.task_create`

Params (`TaskCreateParams`):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `subject` | string | yes | Brief task title (e.g. "Fix authentication bug"). |
| `description` | string | no (default `""`) | What needs to be done. |

```json
{"name": "TaskCreate",
 "arguments": {"subject": "Refactor src/auth/validate.ts",
               "description": "Split into validate_token + load_user"}}
```

The runtime returns the new task's `taskId`.

### `memory.task_update`

Params (`TaskUpdateParams`):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `taskId` | string | yes | Task id returned by `task_create`. |
| `status` | string | yes | `pending` \| `in_progress` \| `completed` \| `blocked`. |

```json
{"name": "TaskUpdate",
 "arguments": {"taskId": "t1", "status": "in_progress"}}
```

### `memory.set_goal`

Params (`SetGoalParams`):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `goal` | string | yes | The session goal in plain language. |

```json
{"name": "memory.set_goal",
 "arguments": {"goal": "Fix the auth bug in src/auth/validate.ts"}}
```

The current goal is rendered at the top of the MEMORY block in every
subsequent system prompt.

### `memory.remember`

Params (`RememberParams`):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `content` | string | yes | The fact to remember in plain text. |

```json
{"name": "Remember",
 "arguments": {"content": "Test command: pytest tests/ -v"}}
```

Stored in **semantic memory** (per-user, per-app), survives context
compaction, and is rendered back into the system prompt at the next
turn (the agent re-reads it like any other prompt section). Secrets
detected in `content` (values matching the redaction patterns -
key, secret, password, token, auth, credential, private, jwt) are
replaced with `[REDACTED]` before storage.

## Memory layers (internal)

declares the data structures
(`MemoryStore`, `MemoryConfig`, `Note`, `Episode`, `Checkpoint`,
`SemanticMemory`, `CachedContent`, `TodoStatus`).

| Layer | Scope | Lifecycle | Backed by |
|-------|-------|-----------|-----------|
| **Working memory** - `goal`, `todos` | per-session | cleared on session end (`cleanup_session`) | `MemoryStore.working` |
| **Episodic** - session events | per-session | cleared on session end | `MemoryStore.episodic` |
| **Semantic** - facts + entity graph | per-user, per-app | persisted to KV backend | `MemoryStore.semantic` (`SemanticMemory`) |
| **Procedural** - learned patterns | per-app | persisted to KV backend | `MemoryStore.procedures` |
| **Cache** - recent file content | per-session | bounded by `limits` | `MemoryStore.cache` (`CachedContent`) |

The runtime maintains one `MemoryStore` per session, keyed by the
**compound `(user_id, session_id)` tuple** - single-key lookup was
a cross-user leak vector and is fixed.

## Session isolation

 Three guarantees verified by the test suite:

- **Per-session working state** - `goal`, `todos`, `episodes`,
  `cache` are scoped by `user_id::session_id`. Two concurrent
  sessions from the same user (or unrelated users sharing a session
  id prefix) never see each other's todos.
- **Per-user semantic memory** - facts written by user A never
  leak into user B's prompt. Earlier versions shared a single
  `_app_semantic` dict; the current code creates a per-user
  `SemanticMemory` lazily, loaded from the KV backend with a
  user-scoped key.
- **Cleanup on session end** - `cleanup_session`
  clears todos, goal, and episodic events when
  the session closes. Semantic + procedural are NOT cleared; they
  persist for the next session.

## Memory injection into the prompt

`get_prompt_sections` returns up to two prompt
sections - the rendered memory snapshot (priority 5) and the memory
instructions (priority 6) - built by
and
`build_memory_instructions`.

The injected MEMORY block looks like:

```
# MEMORY

## Goal
Fix the authentication bug in src/auth/validate.ts

## Todos (3)
- [in_progress] t1: Trace the failing path
- [pending]     t2: Add unit test
- [pending]     t3: Open PR

## Facts (2)
- Test command: pytest tests/ -v
- Project uses FastAPI + SQLAlchemy + Alembic

## Recent activity
... (episodic events)
```

The agent never has to "query" memory - it just reads the prompt.
Calls to `task_create`, `task_update`, `set_goal`, `remember` mutate
the underlying store; the next turn automatically re-renders the
block.

## Configuration knobs

Top-level keys on `tools.modules.memory.config`
(`MemoryModuleConfig`):

| Key | Type | Default | Effect |
|-----|------|---------|--------|
| `working_memory` | bool | `false` | Render goal + todos in the prompt block. |
| `todo_list` | bool | `false` | Enable the todo-list panel. |
| `checkpoint` | bool | `false` | Periodic self-assessment snapshots (advanced). |
| `episodic` | bool | `false` | Record session events. |
| `semantic` | bool \| dict | `{}` | Enable semantic memory. Pass a dict for fine-grained config (vector backend, graph storage, …). |
| `procedural` | bool | `false` | Enable learned patterns layer. |
| `runtime` | dict | `{}` | Proactive injection / content cache / goal guardian knobs (see `MemoryConfig`). |
| `limits` | dict | `{}` | Caps on number of todos / facts / episodes / cached files. |
| `security` | dict | `{}` | `redact_secrets: bool` (default true), `sensitive_patterns: [str]`. |
| `auto_remember` | bool | `false` | If true, the runtime extracts facts from the conversation passively. Off by default - agents should call `Remember` explicitly. |
| `workspace` | string | `""` | Auto-injected by the daemon. Don't set manually. |

The full set of knobs (vector index dim, graph edge limits, cache
TTL, ...) is on `MemoryConfig`.

## Secret redaction

 The redactor scans the value of every
`os.environ[KEY]` whose name matches the sensitive patterns and
replaces matches with `[REDACTED]` in the text before storage.

Default patterns: `key`, `secret`, `password`, `token`, `auth`,
`credential`, `private`, `jwt`. Extend via:

```yaml
tools:
  modules:
    memory:
      config:
        security:
          redact_secrets: true
          sensitive_patterns: [api_key, slack_webhook]
```

Disable explicitly with `redact_secrets: false`.

## What the agent typically does

Common pattern:

1. **Receive a goal** from the user → call `memory.set_goal`.
2. **Plan** → call `memory.task_create` per step (3-7 tasks).
3. **Execute** → before each step, `memory.task_update(status:
   "in_progress")`; after, `memory.task_update(status: "completed")`.
4. **Persist learnings** → `memory.remember` for facts the next
   session should keep (test commands, file locations, gotchas).

The agent never asks "what's my goal?" or "what tasks do I have?" -
the memory block in the prompt already shows them.

## Persistence

`memory.MemoryStore.persist` and `MemoryStore.restore` write/read
the per-user semantic memory to the daemon's KV backend
(). The keying scheme is
`(app_id, user_id)` - facts persist across sessions for the same
(user, app) pair.

There is no separate user-facing API to read/clear memory today.
Operators can clear the KV slice via the database CLI:

```bash
digitorn db inspect            # explore the schema
digitorn db query "DELETE FROM ..."   # use carefully
```

## Cross-references

- Built-in tools index (memory aliases):
  [Built-in Tools](04b-builtin-tools.md#memory-tools-gated-by-toolsmodulesmemory)
- Context window management (compaction trigger, summary brain):
  [Context Management](06-context-management.md)
- Config block reference:
  [App Configuration → tools.modules](02-app-config.md#toolsmodules---module-configuration)
- Per-module reference (storage, advanced knobs):
  [modules/reference/memory.md](../reference/modules/memory.md)
