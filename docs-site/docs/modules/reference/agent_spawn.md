---

id: agent_spawn
title: Agent Spawn Module
sidebar_position: 19
format: md
---




# agent_spawn

Dynamic sub-agent creation and management. Spawn autonomous AI agents that run in parallel, each with their own system prompt, tools, LLM configuration, and objectives. Monitor progress, retrieve results, send messages, or cancel running agents.

| Property | Value |
|----------|-------|
| **Module ID** | `agent_spawn` |
| **Version** | `1.0.0` |
| **Type** | system |
| **Platforms** | All |
| **Dependencies** | None (stdlib only) |
| **Declared Permissions** | `agent.spawn`, `agent.monitor`, `agent.control` |

---


## Actions

### spawn_agent

Create and launch an autonomous sub-agent. The agent runs in parallel with its own LLM loop, tools, and objectives. Returns a `spawn_id` to track the agent.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `name` | string | Yes | - | Human-readable name for the sub-agent |
| `objective` | string | Yes | - | The task/objective for the sub-agent to accomplish |
| `system_prompt` | string | No | `"You are an autonomous AI agent. Complete the given objective."` | System prompt that defines the agent's role and behavior |
| `tools` | array | No | `[]` | List of tools available to the agent (e.g. `["filesystem.read_file", "os_exec.run_command"]`) |
| `model` | string | No | `""` | LLM model to use (default: same as parent) |
| `provider` | string | No | `""` | LLM provider - `anthropic` or `openai` (default: same as parent) |
| `max_turns` | integer | No | `15` | Maximum turns for the agent loop (1--100) |
| `context` | string | No | `""` | Additional context to pass to the agent (files read, previous results, etc.) |

**Returns**: `{"spawn_id": "agent-a1b2c3d4", "name": "...", "status": "running", "message": "..."}`

**Security**: Permission `agent.spawn` / Risk level `medium` / Side effect `process_spawn`

**IML Example**:
```json
{
  "id": "spawn-researcher",
  "action": "spawn_agent",
  "module": "agent_spawn",
  "params": {
    "name": "researcher",
    "objective": "Find all Python files that import asyncio and summarize their purpose",
    "tools": ["filesystem.read_file", "filesystem.list_directory", "filesystem.search_files"],
    "max_turns": 10
  }
}
```

---


### check_agent

Check the current status of a spawned agent.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `spawn_id` | string | Yes | - | The `spawn_id` returned by `spawn_agent` |

**Returns**: `{"spawn_id": "...", "name": "...", "objective": "...", "status": "running|completed|failed|cancelled", "turns": 5, "elapsed_seconds": 12.3}`

When the agent has completed, the response includes an `output_preview` field (first 500 characters). When failed, it includes an `error` field.

**Security**: Permission `agent.monitor` / Risk level `low`

**IML Example**:
```json
{
  "id": "check-researcher",
  "action": "check_agent",
  "module": "agent_spawn",
  "params": {
    "spawn_id": "{{result.spawn-researcher.spawn_id}}"
  },
  "depends_on": ["spawn-researcher"]
}
```

---


### get_result

Get the final result/output of a completed agent. Returns an informational message if the agent is still running.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `spawn_id` | string | Yes | - | The `spawn_id` returned by `spawn_agent` |

**Returns**: `{"spawn_id": "...", "name": "...", "status": "completed", "output": "...", "turns": 8, "duration_seconds": 25.4}`

If the agent is still running, returns `{"status": "running", "message": "Agent is still running. Use wait_agent to block until complete, or check_agent to poll."}`.

Results are also persisted to the KV store (if configured) and can be retrieved after the module restarts.

**Security**: Permission `agent.monitor` / Risk level `low`

**IML Example**:
```json
{
  "id": "get-researcher-result",
  "action": "get_result",
  "module": "agent_spawn",
  "params": {
    "spawn_id": "{{result.spawn-researcher.spawn_id}}"
  },
  "depends_on": ["spawn-researcher"]
}
```

---


### list_agents

List all spawned agents and their current statuses.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `status_filter` | string | No | `"all"` | Filter by status: `all`, `running`, `completed`, `failed`, `cancelled` |

**Returns**: `{"agents": [{"spawn_id": "...", "name": "...", "objective": "...", "status": "...", "turns": N, "elapsed_seconds": N, "tools": [...]}], "total": 3, "running": 1, "completed": 2, "failed": 0}`

**Security**: Permission `agent.monitor` / Risk level `low`

**IML Example**:
```json
{
  "id": "list-running",
  "action": "list_agents",
  "module": "agent_spawn",
  "params": {
    "status_filter": "running"
  }
}
```

---


### cancel_agent

Cancel a running agent. No effect if already completed.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `spawn_id` | string | Yes | - | The `spawn_id` of the agent to cancel |

**Returns**: `{"spawn_id": "...", "cancelled": true}`

If the agent is not running, returns `{"cancelled": false, "reason": "Agent is already completed"}`.

**Security**: Permission `agent.control` / Risk level `medium`

**IML Example**:
```json
{
  "id": "cancel-slow-agent",
  "action": "cancel_agent",
  "module": "agent_spawn",
  "params": {
    "spawn_id": "{{result.spawn-researcher.spawn_id}}"
  }
}
```

---


### wait_agent

Wait for a spawned agent to complete. Blocks until done or timeout is reached. If the agent finishes before the timeout, returns the full result (same as `get_result`).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `spawn_id` | string | Yes | - | The `spawn_id` of the agent to wait for |
| `timeout` | number | No | `300` | Maximum seconds to wait (1--3600) |

**Returns**: Same as `get_result` when the agent completes. On timeout: `{"spawn_id": "...", "status": "running", "message": "Agent still running after 300s timeout. It continues in the background.", "turns": N}`

**Security**: Permission `agent.monitor` / Risk level `low`

**IML Example**:
```json
{
  "id": "wait-for-researcher",
  "action": "wait_agent",
  "module": "agent_spawn",
  "params": {
    "spawn_id": "{{result.spawn-researcher.spawn_id}}",
    "timeout": 120
  },
  "depends_on": ["spawn-researcher"]
}
```

---


### send_message

Send a message to a running agent. The message is delivered into the agent's context between LLM turns via an internal message queue, enabling inter-agent communication.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `spawn_id` | string | Yes | - | The `spawn_id` of the target agent |
| `message` | string | Yes | - | Message to send to the agent |

**Returns**: `{"delivered": true, "queue_size": 1}`

If the agent is not running, returns `{"delivered": false, "reason": "Agent is completed, cannot receive messages"}`.

**Security**: Permission `agent.control` / Risk level `low`

**IML Example**:
```json
{
  "id": "redirect-agent",
  "action": "send_message",
  "module": "agent_spawn",
  "params": {
    "spawn_id": "{{result.spawn-researcher.spawn_id}}",
    "message": "Focus only on files in the src/ directory, skip tests."
  }
}
```

---


## Architecture

Sub-agents are fully autonomous - each has its own LLM loop, tool access, and conversation history. They execute as `asyncio.Task` instances in parallel.

```
Parent Agent (via AgentRuntime)
  |-- calls agent_spawn.spawn_agent(...)
  |     |-- AgentSpawnModule creates an AgentRuntime for the child
  |     |-- Launches it as an asyncio.Task
  |     +-- Returns spawn_id immediately
  |-- calls agent_spawn.check_agent(spawn_id)
  |     +-- Returns status, turn count, elapsed time
  |-- calls agent_spawn.get_result(spawn_id)
        +-- Returns final output when complete
```

A concurrency semaphore limits the number of simultaneously running agents (default: 10). The module persists completed agent results to the KV store (last 100 entries) for retrieval after restarts.

---


## Implementation Notes

- All sub-agents run as `asyncio.Task` instances - no subprocess spawning
- A `asyncio.Semaphore(10)` caps concurrent agents to prevent resource exhaustion
- The `message_queue` (`asyncio.Queue`) delivers inter-agent messages between LLM turns
- Streaming events from sub-agents are captured and optionally propagated to the parent via an event callback
- Completed results are persisted to the KV store under `llmos:agent_spawn:result:<spawn_id>` (capped at 10k characters per result)
- On module shutdown (`on_stop`), all running agent tasks are cancelled
- No external dependencies - uses only Python standard library and the LLMOS Bridge runtime

---


## YAML App Language

An agent with sub-agent spawning capabilities:

```yaml
name: orchestrator
version: "1.0"
description: An orchestrator agent that delegates tasks to sub-agents

model:
  provider: anthropic
  name: claude-sonnet-4-20250514

system_prompt: |
  You are an orchestrator agent. Break complex tasks into sub-tasks and
  delegate them to specialized sub-agents. Monitor their progress and
  combine their results into a final answer.

tools:
  - module: agent_spawn
    action: spawn_agent
  - module: agent_spawn
    action: check_agent
  - module: agent_spawn
    action: get_result
  - module: agent_spawn
    action: list_agents
  - module: agent_spawn
    action: cancel_agent
  - module: agent_spawn
    action: wait_agent
  - module: agent_spawn
    action: send_message
  - module: filesystem
    action: read_file
  - module: filesystem
    action: list_directory
```
