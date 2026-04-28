---

id: builtin-tools
title: Built-in Tools
sidebar_position: 5
format: md
---


# Built-in Tools Reference

Built-in tools are provided by the runtime itself - they don't require any LLMOS module or daemon connection. They are declared in YAML with `builtin:` instead of `module:`.

```yaml
agent:
  tools:
    - builtin: delegate        # multi-agent delegation (sync)
    - builtin: delegate_async  # multi-agent delegation (background)
    - builtin: todo            # task tracking
    - builtin: memory          # multi-level memory
    - builtin: ask_user        # interactive user input
    - builtin: emit            # event bus publishing
    - builtin: send_message    # peer-to-peer messaging
```

## Availability Matrix

Not all builtins work in every context. The runtime injects the appropriate handlers depending on the execution mode.

| Builtin | Single-Agent | Multi-Agent (hierarchical) | Multi-Agent (P2P) | Requires |
|---------|-------------|---------------------------|-------------------|----------|
| `ask_user` | Yes | Coordinator only | No | `input_handler` (CLI mode) |
| `todo` | Yes | Yes (all agents) | Yes | None (KV store optional for persistence) |
| `delegate` | **No** | Coordinator only | No | `CentralizedRuntime` handler |
| `delegate_async` | **No** | Coordinator only | No | `CentralizedRuntime` handler + `AgentSignalQueue` |
| `emit` | Yes | Yes (all agents) | Yes | EventBus (daemon mode) |
| `memory` | Yes | Coordinator only | No | `memory:` block in YAML |
| `send_message` | **No** | **No** | Yes | `PeerToPeerRuntime` handler |

**Key points:**
- `delegate` and `delegate_async` only work inside a `strategy: hierarchical` multi-agent app. In single-agent mode they return an error.
- `delegate_async` launches agents in background and activates watch mode - the coordinator sleeps at zero token cost until signals arrive.
- `send_message` only works in `communication.mode: peer_to_peer`. In other modes it returns an error.
- `todo` works everywhere - it stores tasks in memory (persisted via KV store if connected to daemon).

---


## ask_user

Prompt the user for input. The agent pauses until the user responds.

### YAML Declaration

```yaml
agent:
  tools:
    - builtin: ask_user
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `question` | string | **Yes** | The question to display to the user |

### Return Value

```json
{ "response": "user's answer" }
```

If no input handler is configured (e.g., HTTP mode without interactive terminal):

```json
{ "response": "", "note": "No input handler configured" }
```

### Example

```python
# Agent calls:
ask_user(question="Should I delete the deprecated tests?")
# → {"response": "Yes, go ahead"}
```

### When to Use

- Confirmation before destructive actions (file delete, git push)
- Clarification when the task is ambiguous
- Approval gates in workflows

---


## todo

Persistent task tracking. The agent can create, update, and track tasks across turns and sessions.

### YAML Declaration

```yaml
agent:
  tools:
    - builtin: todo
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `action` | string | **Yes** | One of: `add`, `update`, `complete`, `remove`, `clear_completed`, `list` |
| `task` | string | For `add` | Task description |
| `task_id` | string | For `update`/`complete`/`remove` | 8-character task ID (returned by `add`) |
| `status` | string | For `update` | New status: `pending`, `in_progress`, or `completed` |
| `status_filter` | string | For `list` | Filter by status: `all` (default), `pending`, `in_progress`, `completed` |

### Return Values

**add:**
```json
{ "id": "a1b2c3d4", "task": "Fix login bug", "status": "pending" }
```

**update / complete:**
```json
{ "id": "a1b2c3d4", "task": "Fix login bug", "status": "completed" }
```

**remove:**
```json
{ "removed": true, "task_id": "a1b2c3d4" }
```

**clear_completed:**
```json
{ "cleared": 3, "remaining": 2 }
```

**list:**
```json
{
  "tasks": [
    { "id": "a1b2c3d4", "task": "Fix login bug", "status": "in_progress" },
    { "id": "e5f6g7h8", "task": "Add unit tests", "status": "pending" }
  ],
  "total": 2,
  "pending": 1,
  "in_progress": 1,
  "completed": 0
}
```

### Persistence

Tasks are stored in the KV store under key `llmos:builtins:todos`. When connected to the daemon, tasks persist across sessions. Without a KV store, tasks only last for the current run.

### Example Workflow

```python
# 1. Plan the work
todo(action="add", task="Read the auth module")
todo(action="add", task="Fix the token validation bug")
todo(action="add", task="Run tests")

# 2. Work through tasks
todo(action="update", task_id="a1b2c3d4", status="in_progress")
# ... do the work ...
todo(action="update", task_id="a1b2c3d4", status="completed")

# 3. Check progress
todo(action="list", status_filter="pending")

# 4. Clean up
todo(action="clear_completed")
```

---


## delegate

Delegate a subtask to another agent. **Only available in hierarchical multi-agent mode.** The coordinator agent uses this to route tasks to specialist agents.

### YAML Declaration

```yaml
agents:
  strategy: hierarchical
  agents:
    - id: coordinator
      role: coordinator
      tools:
        - builtin: delegate    # ← coordinator gets this
      system_prompt: |
        Delegate tasks to specialists:
        - delegate(agent_id="coder", task="...")
        - delegate(agent_id="reviewer", task="...")
    - id: coder
      role: specialist
      # specialists do NOT need delegate
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_id` | string | **Yes** | ID of the target agent (must exist in the `agents:` list) |
| `task` | string | **Yes** | Full task description - the specialist receives only this text |

### Return Value

**Success:**
```json
{
  "agent_id": "coder",
  "result": {
    "output": "I've fixed the bug in auth.py by...",
    "success": true
  }
}
```

**Unknown agent:**
```json
{ "agent_id": "unknown_agent", "result": { "error": "Unknown agent: unknown_agent" } }
```

**Not in multi-agent mode:**
```json
{ "error": "Delegation not available (single-agent mode)" }
```

### How It Works Internally

1. The `CentralizedRuntime` creates a `delegate_handler` closure
2. It injects this handler into the coordinator's `BuiltinToolExecutor`
3. When the LLM calls `delegate(agent_id, task)`:
   - The handler looks up the target agent
   - Creates an isolated `AgentRuntime` for the specialist
   - Runs the specialist with the task as input
   - Returns the specialist's output to the coordinator
4. The coordinator sees the result as a tool response and continues

### Best Practices

- **Be specific**: The specialist only sees the `task` text - include all context it needs
- **Include file paths**: "Fix the bug in `src/auth.py` line 42" not "Fix the auth bug"
- **Delegate to the right agent**: Don't send code writing to a reviewer
- **Chain delegations**: Delegate to researcher first, then use findings to delegate to coder

---


## delegate_async

Delegate a subtask to another agent **asynchronously** (background execution). **Only available in hierarchical multi-agent mode.** The agent is launched as a background `asyncio.Task`. The coordinator receives an immediate response with a `spawn_id` and can continue working or enter **watch mode** (zero LLM tokens) until the agent completes.

### YAML Declaration

```yaml
agents:
  strategy: hierarchical
  agents:
    - id: coordinator
      role: coordinator
      tools:
        - builtin: delegate_async    # ← async delegation
        - builtin: delegate          # ← can also have sync delegation
      watch:
        auto: true                   # ← enables watch mode (default)
        timeout: "10m"
      system_prompt: |
        You can launch agents in the background:
        - delegate_async(agent_id="researcher", task="...")
        - delegate_async(agent_id="coder", task="...")
        Then stop responding - you'll receive signals when they finish.
    - id: researcher
      role: specialist
    - id: coder
      role: specialist
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_id` | string | **Yes** | ID of the target agent (must exist in the `agents:` list) |
| `task` | string | **Yes** | Full task description - the specialist receives only this text |

### Return Value

**Success (immediate):**
```json
{
  "launched": true,
  "agent_id": "researcher",
  "spawn_id": "spawn-researcher-a1b2c3d4",
  "note": "Agent 'researcher' is now running in the background. You will receive an AGENT_COMPLETED signal when it finishes."
}
```

**Unknown agent:**
```json
{ "error": "Unknown agent: unknown_agent" }
```

**Not in multi-agent mode:**
```json
{ "error": "Async delegation not available (requires hierarchical multi-agent mode with watch enabled)" }
```

### Signal on Completion

When the background agent finishes, an `AGENT_COMPLETED` signal is injected into the coordinator's conversation:

```text
[AGENT COMPLETED] Agent 'researcher' finished (spawn_id=spawn-researcher-a1b2c3d4, 5 turns, 12.3s).
Result: The project uses a monorepo structure with...
```

On failure, an `AGENT_FAILED` signal is injected instead.

### How It Works Internally

1. The `CentralizedRuntime` creates an `AgentSignalQueue` for the coordinator
2. When the LLM calls `delegate_async(agent_id, task)`:
   - A `spawn_id` is generated and tracked in the signal queue
   - The target agent is launched via `asyncio.create_task`
   - The coordinator receives an immediate response with the `spawn_id`
3. The coordinator can:
   - **Continue working** - make more tool calls, delegate more agents
   - **Stop responding** - the runtime detects active signal sources and enters watch mode
4. When the background agent completes:
   - An `AGENT_COMPLETED` signal is pushed to the coordinator's signal queue
   - The signal queue wakes up the coordinator
   - The signal is injected as a message into the coordinator's conversation
   - The coordinator gets a new LLM turn to process the result
5. If the coordinator finishes before background agents complete, they are cancelled

### Example: Parallel Research

```python
# Launch two agents in parallel
delegate_async(agent_id="researcher", task="Analyze the auth module")
delegate_async(agent_id="coder", task="Write unit tests for auth")
# Stop - watch mode activates, 0 tokens until signals arrive

# ... time passes ...

# [AGENT COMPLETED] researcher finished: "The auth module uses JWT..."
# [AGENT COMPLETED] coder finished: "Created 5 test files..."

# Coordinator now has both results and can synthesize
```

### delegate vs delegate_async

| Aspect | `delegate` | `delegate_async` |
|--------|-----------|-------------------|
| **Execution** | Synchronous - blocks until agent finishes | Asynchronous - returns immediately |
| **Parallelism** | Sequential only | Multiple agents in parallel |
| **Token cost** | Coordinator idle but context held | Watch mode - zero tokens while waiting |
| **Best for** | Simple task chains | Complex workflows, parallel exploration |

---


## emit

Publish an event to the LLMOS event bus. Other agents, triggers, or external systems can subscribe to these events.

### YAML Declaration

```yaml
agent:
  tools:
    - builtin: emit
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `topic` | string | **Yes** | Event topic (e.g., `"app.progress"`, `"app.lint"`) |
| `data` | object | No | Event payload (any JSON-serializable object) |

### Return Value

**With event bus:**
```json
{ "published": true, "topic": "app.progress" }
```

**Without event bus (no daemon):**
```json
{ "published": false, "note": "No event bus configured" }
```

### Example

```python
# Signal progress
emit(topic="app.progress", data={"step": "analysis", "progress": 0.5})

# Signal completion
emit(topic="app.task_complete", data={"task_id": "abc", "duration_ms": 1500})

# Trigger downstream workflows
emit(topic="app.lint", data={"path": "src/auth.py", "status": "clean"})
```

### Integration with Triggers

Events emitted by `emit` can be consumed by `event` triggers:

```yaml
triggers:
  - id: on_lint_complete
    type: event
    topic: "app.lint"
    transform: "Lint result: {{payload.status}} for {{payload.path}}"
```

---


## memory

Multi-level memory operations. Gives the agent direct control over persistent memory.

### YAML Declaration

```yaml
agent:
  tools:
    - builtin: memory

memory:                              # ← required for memory to work
  working:
    max_size: "100MB"
  conversation:
    max_history: 500
  project:
    path: "{{workspace}}/.llmos/MEMORY.md"
  episodic:
    auto_record: true
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `action` | string | **Yes** | One of: `store`, `recall`, `search`, `list` |
| `level` | string | No | Memory level: `working` (default), `conversation`, `project`, `episodic` |
| `key` | string | For `store`/`recall` | Key for the memory entry |
| `value` | string | For `store` | Value to store |
| `query` | string | For `search` | Semantic search query (episodic only) |
| `top_k` | integer | No | Number of search results (default: 5) |

### Memory Levels

| Level | Scope | Persistence | Best For |
|-------|-------|-------------|----------|
| `working` | Current run | In-memory (lost on exit) | Temporary scratch data, intermediate results |
| `conversation` | Across sessions | KV store (daemon) | Project context, user preferences, known paths |
| `project` | Across sessions | Markdown file | Human-readable project notes, conventions |
| `episodic` | Across sessions | Semantic store | Past experiences, searchable by similarity |

### Return Values

**store (working/conversation):**
```json
{ "stored": true, "level": "working", "key": "findings" }
```

**store (episodic):**
```json
{ "stored": true, "level": "episodic", "episode_id": "ep-a1b2c3d4" }
```

**recall (found):**
```json
{ "level": "working", "key": "findings", "value": "The auth module uses JWT...", "found": true }
```

**recall (not found):**
```json
{ "level": "working", "key": "missing_key", "value": null, "found": false }
```

**search:**
```json
{ "results": [...], "count": 3 }
```

**list:**
```json
{ "level": "working", "keys": ["findings", "project_structure", "current_task"] }
```

### Example Workflow

```python
# At session start - recall context
memory(action="recall", level="conversation", key="project_context")

# During work - store findings
memory(action="store", level="working", key="analysis", value="The bug is in token validation...")

# At session end - persist important context
memory(action="store", level="conversation", key="project_context",
       value="Monorepo with 2 packages. Uses Poetry for Python, npm for dashboard.")

# Search past experiences
memory(action="search", query="how did I fix the auth bug last time?", top_k=3)
```

See [Memory](memory) for full details on memory architecture and configuration.

---


## send_message

Send a direct message to another agent in peer-to-peer communication mode. **Only available when `communication.mode: peer_to_peer`.**

### YAML Declaration

```yaml
agents:
  communication:
    mode: peer_to_peer
    topology: ring                   # mesh | ring | star
  agents:
    - id: researcher
      tools:
        - builtin: send_message      # ← each P2P agent gets this
      system_prompt: |
        Send findings to the analyst:
        send_message(target="analyst", message="My findings: ...")
    - id: analyst
      tools:
        - builtin: send_message
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `target` | string | **Yes** | ID of the target agent |
| `message` | string | **Yes** | Message content |

Aliases: `agent_id` is accepted for `target`, `content` is accepted for `message`.

### Return Value

**Success:**
```json
{ "sent": true }
```

**Not in P2P mode:**
```json
{ "error": "P2P messaging not available (not in peer_to_peer mode)" }
```

**Topology violation (e.g., ring topology, non-adjacent agent):**
```json
{ "error": "Cannot send from 'agent_a' to 'agent_c': not allowed by ring topology" }
```

### Topology Constraints

The `ChannelManager` enforces routing rules based on topology:

| Topology | Rule | Example (3 agents: A, B, C) |
|----------|------|------|
| `mesh` | Any agent can message any other agent | A↔B, A↔C, B↔C |
| `ring` | Each agent can only message the next agent | A→B, B→C, C→A |
| `star` | Hub (1st agent) talks to all; spokes only talk to hub | Hub↔B, Hub↔C, B✗C |

### How It Works

1. The `PeerToPeerRuntime` creates a `ChannelManager` with the configured topology
2. All agents run concurrently via `asyncio.gather`
3. When agent A calls `send_message(target="B", message="...")`:
   - The `ChannelManager` validates the route against the topology
   - If allowed, the message is queued in agent B's inbox
   - Agent B receives it as an injected system message on its next turn
4. Agents continue until all have completed or max rounds reached

> **Note:** If `send_message` is not declared in an agent's tools but the communication mode is `peer_to_peer`, the compiler auto-injects it at runtime with an info-level warning.

---


## Comparison: Builtins vs Modules

| Aspect | Builtins | Modules |
|--------|----------|---------|
| **Declaration** | `- builtin: delegate` | `- module: filesystem` |
| **Source** | Hardcoded in runtime | Plugin system (daemon) |
| **Availability** | Always (context-dependent) | Requires daemon or standalone mode |
| **Security** | No permission pipeline | Full 15-step security pipeline |
| **Parameters** | Fixed schema | Schema from module manifest |
| **Examples** | `delegate`, `delegate_async`, `todo`, `memory` | `filesystem`, `os_exec`, `api_http` |

Builtins handle **runtime coordination** (delegation, messaging, memory, task tracking). Modules handle **external actions** (file I/O, shell commands, HTTP, databases).

---


## Source Code Reference

| Component | File | Description |
|-----------|------|-------------|
| Tool schemas | `apps/tool_registry.py` (`_BUILTIN_TOOLS`) | Parameter definitions sent to LLM |
| Execution logic | `apps/builtins.py` (`BuiltinToolExecutor`) | Handler implementations |
| Handler injection | `apps/multi_agent/centralized.py` | `delegate_handler` + `delegate_async_handler` for hierarchical mode |
| Handler injection | `apps/multi_agent/decentralized.py` | `send_message_handler` for P2P mode |
| Registration | `apps/agent_runtime.py` | Routes tool calls to builtin executor |
