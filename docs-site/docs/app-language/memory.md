---

id: memory
title: Memory
sidebar_position: 6
format: md
---


# Memory

LLMOS provides a multi-level memory system. Each level serves a different purpose, with different persistence, speed, and capacity characteristics.

## Memory Levels

```
Working Memory (fast, ephemeral)
    ↓
Conversation Memory (persisted, per-session)
    ↓
Project Memory (file-based, human-readable)
    ↓
Episodic Memory (vector-indexed, cross-session)
    ↓
Procedural Memory (learned patterns)
```

## Storage Backends

| Backend | Description | Used By |
|---------|-------------|---------|
| `in_memory` | Fast, ephemeral, lost on restart | working |
| `sqlite` | Persistent SQLite database | conversation, procedural |
| `chromadb` | Vector database for semantic search | episodic |
| `file` | Plain file (Markdown) | project |
| `redis` | Redis-backed (distributed mode) | any level |

## Configuration

```yaml
memory:
  working:
    backend: in_memory               # in_memory (default)
    max_size: "100MB"

  conversation:
    backend: sqlite                  # sqlite (default)
    path: "{{data_dir}}/conversations.db"
    max_history: 1000
    auto_summarize: true
    summarize_after: 50              # Summarize after N messages

  episodic:
    backend: chromadb                # chromadb (default)
    path: "{{data_dir}}/episodes"
    auto_record: true                # Record episodes automatically
    record_fields: [input, actions_taken, outcome, lessons]
    auto_recall:
      on_start: true                 # Recall relevant episodes on start
      query: "{{trigger.input}}"     # Search query
      limit: 5                       # Max episodes to recall
      min_similarity: 0.7            # Minimum similarity threshold (0-1)

  project:
    backend: file                    # file (default)
    path: "{{workspace}}/.llmos/MEMORY.md"
    auto_inject: true                # Inject into system prompt
    agent_writable: true             # Agent can update this file
    max_lines: 200

  procedural:
    backend: sqlite
    path: "{{data_dir}}/procedures.db"
    learn_from_failures: true
    learn_from_successes: true
    auto_suggest: true
```

## Working Memory

Fast, in-memory key-value store. Lives only for the duration of a run.

```yaml
memory:
  working:
    backend: in_memory
    max_size: "50MB"
```

**Agent usage:**
```
memory(action="store", level="working", key="findings", value="...")
memory(action="recall", level="working", key="findings")
memory(action="list", level="working")
```

Best for: intermediate results, scratch data, computation state.

## Conversation Memory

Persists conversation history across turns. Enables multi-turn interactions.

```yaml
memory:
  conversation:
    backend: sqlite
    max_history: 100                 # Max messages to keep
    auto_summarize: true             # Summarize older messages
    summarize_after: 50              # Trigger after N messages
```

**Backends:** `sqlite` (default), `in_memory`

The conversation history is automatically injected into the LLM context. When `auto_summarize` is enabled, older messages are summarized to save tokens.

**Agent usage:**
```
memory(action="store", level="conversation", key="review_status", value="in_progress")
memory(action="recall", level="conversation", key="review_status")
```

## Episodic Memory

Vector-indexed long-term memory. Records episodes (completed tasks) and enables semantic search across sessions.

```yaml
memory:
  episodic:
    backend: chromadb
    path: "{{data_dir}}/episodes"
    auto_record: true
    record_fields:
      - input              # What was asked
      - actions_taken       # What the agent did
      - outcome            # What happened
      - lessons            # What was learned
    auto_recall:
      on_start: true
      query: "{{trigger.input}}"
      limit: 5
      min_similarity: 0.7
```

**Backends:** `chromadb` (default, requires `pip install chromadb`)

When `auto_record` is enabled, each completed run is automatically stored as an episode via `_auto_record_episode()`. The recorded episode includes:

- **Input** - The first 500 characters of the user input
- **Outcome** - Success or failure status
- **Output** - The first 500 characters of the agent's output
- **Metadata** - App name, turns, tokens, duration

When `auto_recall.on_start` is enabled, relevant past episodes are injected into the agent's context at the start of each run. Episodes are filtered by `min_similarity` (distance-based: lower = more similar).

**Agent usage:**
```
# Store an episode manually
memory(action="store", level="episodic", key="auth-review", value="Found XSS vulnerability in...")

# Search past episodes
memory(action="search", query="security vulnerabilities", top_k=5)
```

## Project Memory

A persistent Markdown file that serves as the agent's long-term project knowledge. Human-readable and editable.

```yaml
memory:
  project:
    path: "{{workspace}}/.llmos/MEMORY.md"
    auto_inject: true                # Inject into system prompt
    agent_writable: true             # Allow agent to update
    max_lines: 200
```

**Backends:** `file` (default)

When `auto_inject` is true, the file contents are included in the system prompt on every LLM call. When `agent_writable` is true, the agent can add entries using the memory tool.

**Agent usage:**
```
# Read project memory
memory(action="recall", level="project")

# Add to project memory
memory(action="store", level="project", key="Architecture", value="The app uses a microservices...")
```

The file format is Markdown with `## Key` sections:

```markdown
## Architecture
The app uses a microservices pattern...

## Known Issues
- Login timeout on slow connections
```

## Procedural Memory

Learned patterns from past successes and failures. The runtime **automatically learns** from every tool execution and can suggest relevant procedures to the agent.

```yaml
memory:
  procedural:
    backend: sqlite
    path: "{{data_dir}}/procedures.db"
    learn_from_failures: true        # Record what went wrong
    learn_from_successes: true       # Record what worked
    auto_suggest: true               # Suggest procedures to agent
```

### Auto-Learning

When procedural memory is configured, the runtime automatically records a procedure entry **after every tool call**. Each entry includes:

- **Pattern** - The module, action, and first 3 parameters (e.g. `filesystem.read_file(path='/src/main.py')`)
- **Outcome** - `"success"` or the error message (truncated to 200 chars)
- **Success flag** - Whether the call succeeded
- **Context** - Duration, module, and action metadata

Entries are stored in KV store with a `procedural:` prefix and an index is maintained for fast retrieval.

### Auto-Suggest

When `auto_suggest: true`, the runtime calls `suggest_procedures()` at the start of each run. It performs keyword matching between the user's input and stored procedure patterns, returning the top 5 matches. These are injected into the agent's system prompt as "Learned Procedures":

```text
## Learned Procedures
- [SUCCESS] filesystem.read_file(path='/config.yaml') → success
- [FAILURE] os_exec.run_command(command=['npm', 'test']) → exit code 1
```

This gives the agent awareness of past successes and failures for similar tasks.

## Cognitive Persistence

When the `memory` module is connected (daemon mode), the agent gains access to cognitive state management:

```python
# Set an objective (NEVER forgotten, auto-injected into every prompt)
memory.set_objective(goal="Fix the authentication bug", sub_goals=["Read auth code", "Write fix", "Run tests"])

# Get full cognitive state
memory.get_context()

# Update progress
memory.update_progress(progress=0.5)

# Observe all memory state across all backends
memory.observe()
```

The objective is permanently injected into the agent's context. It is NEVER truncated or compressed. This ensures the agent always knows what it's working on, even across long conversations.

## Memory in System Prompt

Reference memory values in the system prompt using template expressions:

```yaml
agent:
  system_prompt: |
    Previous review: {{memory.last_review_summary}}
    Known issues: {{memory.known_issues}}
```

## Complete Example

```yaml
memory:
  working:
    max_size: "50MB"
  conversation:
    max_history: 100
  project:
    path: "{{workspace}}/.llmos/MEMORY.md"
    auto_inject: true
    agent_writable: true
    max_lines: 200
  episodic:
    auto_record: true
    auto_recall:
      on_start: true
      limit: 5
```
