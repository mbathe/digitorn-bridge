---
id: memory
---

# Cognitive Memory System

The memory module gives agents persistent cognitive awareness. It makes agents methodical, organized, and resilient to context compaction. The agent always knows what it's doing, what it has found, and what remains.

## Architecture

```
                          Always visible in system prompt
                          (survives every compaction)

+------------------------------------------------------------------+
|                      WORKING MEMORY                               |
|                                                                   |
|  Original request    "Audit the MCP module security..."           |
|  Goal                "Complete security audit of MCP module"      |
|  Status              In progress: Analyze oauth.py [t4]           |
|  Tasks (5/12)        t1 done, t2 done, ... t4 in_progress        |
|  Notes (2 pending)   n1: remove debug prints, n2: revert config  |
|  Last checkpoint     "5 files analyzed, 2 vulns found, 7 remain" |
|  Key facts           "oauth.py uses PKCE", "no hardcoded creds"  |
|  Active context      oauth.py (420 lines), security.py (89 lines)|
+------------------------------------------------------------------+

                          Per-app shared
+------------------------------------------------------------------+
|  SEMANTIC MEMORY     Facts + entity relationship graph            |
|  EPISODIC MEMORY     Past session summaries                       |
|  PROCEDURAL MEMORY   Learned patterns                             |
+------------------------------------------------------------------+
```

## Quick Start

```yaml
modules:
  memory:
    config:
      working_memory: true    # goal, plan, facts, entities
      todo_list: true         # task tracking with progress
      checkpoint: true        # save points for self-assessment
      semantic:
        vector: true          # fact storage with search
        graph: true           # entity relationships
      episodic: true          # past session summaries
      procedural: true        # learned patterns
      runtime:
        goal_guardian: true    # reminds agent if it drifts
        content_cache: true   # O(1) file access after first read
```
Every layer is opt-in. Enable only what you need.

## Layers

### Working Memory

Always present in the system prompt. Survives every compaction. Contains:

- **Original request** -- the user's exact words, preserved verbatim
- **Goal** -- what the agent is trying to achieve
- **Plan** -- step-by-step approach (auto-creates tasks)
- **Tasks** -- progress tracker with status (pending/in_progress/done/blocked)
- **Notes** -- sticky reminders the agent must not forget
- **Checkpoints** -- save points with self-assessment
- **Key facts** -- important findings (auto-saved from completed tasks)
- **Active context** -- files and entities the agent is working with

```yaml
modules:
  memory:
    config:
      working_memory: true
```
### Todo List

Task tracking integrated with the plan. When the agent calls `set_plan`, a task is automatically created for each step.

```yaml
modules:
  memory:
    config:
      todo_list: true
```
The agent tracks progress with `update_todo`:
- `pending` -- not started
- `in_progress` -- currently working on it
- `done` -- completed (with result notes that become key facts)
- `blocked` -- stuck (with reason)

The user sees real-time progress in the CLI:
```
[========----------] 5/12 (42%)
  done  [t1] List files -- 17 Python files found
  done  [t2] Analyze security.py -- no critical issues
  done  [t3] Analyze oauth.py -- PKCE, moderate risk
  done  [t4] Analyze connections.py -- pool management OK
  done  [t5] Analyze __init__.py -- simple, no risk
  >>    [t6] Analyze module.py  <-- NEXT
        [t7] Analyze transports.py
        [t8] Final report
```

### Sticky Notes

Reminders the agent makes to itself -- things it must not forget before finishing.

```yaml
modules:
  memory:
    config:
      working_memory: true   # notes are part of working memory
```
Use cases:
- "Remove debug prints before finishing"
- "Revert timeout from 5s back to 30s"
- "Re-check connections.py after understanding module.py"
- "Ask the user about the behavior on line 42"

Notes are always visible in the memory snapshot and survive compaction. The system reminds the agent if it tries to finish with unresolved notes.

### Checkpoints

Save points where the agent summarizes its progress. Like a "save game" that survives compaction.

```yaml
modules:
  memory:
    config:
      checkpoint: true
```
The agent calls `checkpoint` when it finishes a significant phase:
```
Last checkpoint [cp1]:
  Done so far: Analyzed 5 critical files (security.py, oauth.py, connections.py)
  Key findings: 2 moderate risks found, no critical vulnerabilities
  Remaining: 7 files to analyze + final report
```

Only the last 3 checkpoints are kept to avoid bloat.

### Semantic Memory (shared across sessions)

Facts and entity relationships shared across all sessions of the same app.

```yaml
modules:
  memory:
    config:
      semantic:
        vector: true    # fact storage
        graph: true     # entity relationships
```
- Facts: `add_fact("Project uses Python 3.12")` -- searchable via `recall`
- Graph: `add_relationship("auth.py", "imports", "oauth.py")` -- entity connections

Semantic memory is per-app (not per-session) -- facts learned by one user session are visible to all.

### Episodic Memory

Summaries of past sessions.

```yaml
modules:
  memory:
    config:
      episodic: true
```
### Procedural Memory

Learned patterns from past work.

```yaml
modules:
  memory:
    config:
      procedural: true
```
## Actions (16 total)

### Planning
| Action | Description |
|--------|-------------|
| `set_goal` | Define the main objective |
| `set_plan` | Create a step-by-step plan (auto-creates tasks) |
| `update_plan_step` | Advance to a specific step |

### Task Management
| Action | Description |
|--------|-------------|
| `add_todo` | Add a task (with optional `after_id` for positioning) |
| `update_todo` | Change task status (pending/in_progress/done/blocked) |

### Notes & Checkpoints
| Action | Description |
|--------|-------------|
| `note` | Add a sticky reminder |
| `resolve_note` | Mark a note as done |
| `checkpoint` | Save a progress checkpoint |

### Knowledge
| Action | Description |
|--------|-------------|
| `add_fact` | Store an important finding |
| `track_entity` | Track a file/person/concept in active context |
| `add_relationship` | Add an entity relationship to the graph |
| `recall` | Search memory for relevant facts |
| `forget` | Remove a fact |

### Utility
| Action | Description |
|--------|-------------|
| `get_snapshot` | Get the full memory state |
| `cache_content` | Cache file content for O(1) access |
| `add_episode` | Record a session summary |

## Automatic Behaviors

The memory system includes hooks that fire automatically during the agent loop:

### On turn start
- Activates the correct session store (per-user isolation)
- Updates the memory snapshot in the system prompt
- Captures the original user request (turn 0 only)
- Session resume detection -- if returning to an existing session, injects a resume notice
- Goal guardian -- reminds the agent if no goal is set (if enabled)

### On tool result
- Auto-tracks files as active entities when `filesystem.read` is called
- Auto-caches file content for O(1) access (if content_cache enabled)

### On turn end
- Checks for goal drift (if goal_guardian enabled)

### On compaction
- Reinjects the full memory snapshot (goal, tasks, notes, checkpoints, facts)
- The agent never loses its cognitive state

### On completion
- If the agent tries to finish with incomplete tasks or unresolved notes, it gets a reminder
- Not a blocker -- just information

### Async events
When watchers, scheduled jobs, or background tasks complete, their results are automatically stored as key facts:
- Watcher notification -> key fact with result
- `remember()` reminder -> auto-creates a todo
- Background task completion -> key fact with result

## Adaptive Behavior

The system prompt instructions adapt to the task complexity. The agent decides:

- **Simple question** (greeting, factual answer): respond directly, no memory tools needed
- **Medium task** (read a file, fix a bug): set a goal, work, store findings
- **Complex task** (audit, refactoring, multi-file analysis): full workflow with goal, plan, tasks, checkpoints

This guidance is injected automatically by the context builder when the memory module is active -- no need to write it in the app's system prompt.

## Session Isolation

Memory is scoped by app and session:

| Layer | Scope | Why |
|-------|-------|-----|
| Working memory (goal, plan, todos, notes) | Per session | Each user has their own work |
| Episodic memory | Per session | Each session has its own history |
| Semantic facts + graph | Per app (shared) | Project knowledge is shared |
| Procedural patterns | Per app (shared) | Learned patterns are shared |

Two users chatting with the same app simultaneously have completely independent working memory.

## Session Resume and Fork

The SessionStore supports message persistence for resuming interrupted sessions and forking sessions into parallel branches.

### Resume

When a session is interrupted (timeout, disconnect, server restart), the full conversation messages can be persisted and reloaded:

```python
# Persisted automatically if enabled in config
session_store.save_messages(app_id, session_id, messages)

# Reload on resume
messages = session_store.load_messages(app_id, session_id)
```

Combined with the memory system, the agent resumes exactly where it left off -- working memory (goal, tasks, notes, facts) is restored from the memory store, and messages are reloaded from the session store.

### Fork

Fork creates a copy of the current session state under a new ID. Both sessions continue independently:

```python
session_store.fork(app_id, source_session_id, new_session_id)
```

Use case: the user wants to try two approaches to the same problem. Fork the session after the analysis phase, then each branch explores a different solution.

### Configuration

```yaml
app:
  session:
    persist_messages: true    # save messages for resume (default: false)
    ttl: 3600                 # session lifetime in seconds
```
## File Checkpointing

The filesystem module supports file checkpointing -- automatic snapshots before every write/edit/insert. This provides a safety net for undo operations.

### Enable

```yaml
modules:
  filesystem:
    config:
      checkpoint: true         # enable checkpointing (default: false)
      max_checkpoints: 10      # max snapshots per file (default: 10)
```
### Actions

| Action | Description |
|--------|-------------|
| `undo` | Restore a file to its previous state (pops the last snapshot) |
| `list_checkpoints` | List all files with checkpoint counts |
| `diff_checkpoint` | Show diff between current file and last checkpoint |

### How it works

Every time `write`, `edit`, or `insert` is called, the current file content is saved as a snapshot **before** the modification. The snapshots are stored in memory (not persisted to disk) and automatically evicted when the limit is reached.

```
Agent: filesystem.write("config.yaml", new_content)
  -> System saves current config.yaml as checkpoint
  -> Writes new content
  -> Returns { path: "config.yaml", checkpoint: "cp_1" }

Agent: filesystem.undo(path="config.yaml")
  -> Restores the saved snapshot
  -> Returns { path: "config.yaml", restored_bytes: 1234 }
```

## Project Memory

Project memory is a file loaded automatically into the agent's system prompt at startup. It contains project-specific conventions, architecture decisions, and instructions that persist across all sessions.

Unlike cognitive memory (which is managed by the agent during conversations), project memory is a static file written by the developer or maintained between sessions. It serves as the project's "source of truth" that every agent in the application sees.

### Configuration

```yaml
execution:
  project_memory: auto              # default: auto-detect
```
| Value | Behavior |
|-------|----------|
| `auto` (default) | Scans the workspace for `.digitorn.md` then `CLAUDE.md`. Loads the first one found. |
| `"path/to/file.md"` | Loads that specific file, relative to the workspace root. |
| `""` (empty string) | Disabled. No project memory file is loaded. |

### Examples

```yaml
# DevOps agent -- load the operations runbook
execution:
  workspace: "/opt/infrastructure"
  project_memory: "docs/RUNBOOK.md"

# Data science agent -- load the dataset documentation
execution:
  workspace: "{{env.PROJECT_DIR}}"
  project_memory: "notebooks/README.md"

# Simple chatbot -- no project memory needed
execution:
  project_memory: ""
```
### How it works

At bootstrap, the framework resolves the workspace directory, then looks for the configured project memory file. If found, its content is prepended to the agent's system prompt under a "Project Memory" header.

The project memory is visible to the agent at every turn, including after context compaction. It is never summarized or truncated -- it is part of the system prompt.

### Best practices

- Keep the file concise. Every line consumes context window tokens at every turn.
- Focus on conventions and decisions that the agent must always follow.
- Do not duplicate information that the agent can discover by reading code.
- Update the file when project conventions change.

## Complete Configuration Reference

```yaml
modules:
  memory:
    config:
      # --- Core layers (all opt-in, default: false) ---
      working_memory: true    # goal, plan, original request, key facts, entities
      todo_list: true         # task tracking with auto-create from plan
      checkpoint: true        # save points for self-assessment

      # --- Semantic memory (per-app, shared across sessions) ---
      semantic:
        vector: true          # fact storage, searchable via recall()
        graph: true           # entity relationship graph

      # --- Long-term memory ---
      episodic: true          # past session summaries
      procedural: true        # learned patterns from past work

      # --- Runtime behaviors ---
      runtime:
        goal_guardian: true         # reminds agent if no goal set or drifting
        content_cache: true         # auto-cache file contents on read (O(1) access)
        proactive_injection: false  # inject relevant memories before LLM call
        background_extraction: false # extract facts from conversations in background

      # --- Limits ---
      limits:
        max_injected: 7        # max key facts shown in snapshot (human working memory ~7)
        budget_percent: 15     # max % of context window for memory
        max_memories: 1000     # max stored memories per app
        episodic_ttl_days: 90  # auto-archive episodes after 90 days
```
### Config details

| Config | Type | Default | Description |
|--------|------|---------|-------------|
| `working_memory` | bool | false | Enable goal, plan, facts, entities, original request |
| `todo_list` | bool | false | Enable task tracking with progress |
| `checkpoint` | bool | false | Enable save-point self-assessment |
| `semantic.vector` | bool | false | Enable fact storage with keyword search |
| `semantic.graph` | bool | false | Enable entity relationship graph |
| `episodic` | bool | false | Enable session history summaries |
| `procedural` | bool | false | Enable learned pattern storage |
| `runtime.goal_guardian` | bool | false | Inject goal reminders when agent drifts |
| `runtime.content_cache` | bool | false | Auto-cache file reads for O(1) re-access |
| `runtime.proactive_injection` | bool | false | Pre-load relevant memories before LLM call |
| `runtime.background_extraction` | bool | false | Extract facts from conversations asynchronously |
| `limits.max_injected` | int | 7 | Max key facts in memory snapshot |
| `limits.budget_percent` | int | 15 | Max % of context window for memory injection |
| `limits.max_memories` | int | 1000 | Max stored facts per app |
| `limits.episodic_ttl_days` | int | 90 | Auto-archive episodes after N days |

### Minimal config (just task tracking)

```yaml
modules:
  memory:
    config:
      working_memory: true
      todo_list: true
```
### Recommended config (most use cases)

```yaml
modules:
  memory:
    config:
      working_memory: true
      todo_list: true
      checkpoint: true
      semantic:
        vector: true
      runtime:
        goal_guardian: true
        content_cache: true
```
### Full power config

```yaml
modules:
  memory:
    config:
      working_memory: true
      todo_list: true
      checkpoint: true
      semantic:
        vector: true
        graph: true
      episodic: true
      procedural: true
      runtime:
        goal_guardian: true
        content_cache: true
```
## Full YAML Example

```yaml
app:
  app_id: code-analyst
  name: "Code Analyst"

modules:
  memory:
    config:
      working_memory: true
      todo_list: true
      checkpoint: true
      semantic:
        vector: true
        graph: true
      runtime:
        goal_guardian: true
        content_cache: true
  filesystem:
    config:
      sources:
        workspace: "{{workspace}}"

variables:
  workspace: "{{env.PWD}}"

agents:
  - id: analyst
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
      context:
        max_tokens: 50000
        output_reserved: 4000
        strategy: summarize
        keep_recent: 6
        auto_compact: true
    system_prompt: |
      You are a senior software architect.

execution:
  mode: conversation
```
Note: the system prompt is minimal. All memory-related behavior (planning methodology, task tracking, checkpoint guidance) is auto-injected by the context builder based on which memory layers are active.
