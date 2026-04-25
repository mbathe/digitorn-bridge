---
id: 04b-builtin-tools
---

# System Tools Reference

System tools are provided by the runtime itself. They are always available to agents (no module declaration needed) and are injected automatically by the `context_builder` based on the app configuration.

## Tool Categories

System tools are organized in three categories, each activated by YAML configuration:

| Category | Activated by | Description |
|----------|-------------|-------------|
| **Primitives** | Always available | Parallel execution, background tasks |
| **Memory** | `modules.memory` | Goal, plan, todos, notes, facts, checkpoints |
| **Agent Spawn** | `agents` with coordinator + specialists | Spawn, monitor, and manage sub-agents |

In **discovery mode**, system tools are injected as direct tools alongside the meta-tools (search_tools, execute_tool, etc.). In **direct mode**, they are injected alongside the module tools.

## Primitives (always available)

### Execution

| Tool | Description |
|------|-------------|
| `run_parallel` | Execute multiple actions in parallel (asyncio.gather) |
| `background_run` | Launch any action as a background task |
| `background_status` | Check background task status |
| `background_result` | Get the result of a completed background task |
| `background_cancel` | Cancel a running background task |
| `background_list` | List all background tasks |
| `background_wait` | Wait for a background task to complete |

### Watchers (when `execution.watchers: true`)

| Tool | Description |
|------|-------------|
| `watch_start` | Start a persistent watcher (periodic tool execution) |
| `watch_stop` | Stop a watcher |
| `watch_pause` | Pause a running watcher |
| `watch_resume` | Resume a paused watcher |
| `watch_status` | Get watcher status and metrics |
| `watch_list` | List all watchers |
| `watch_history` | Get recent check results |

### Scheduler (when `execution.scheduler: true`)

| Tool | Description |
|------|-------------|
| `schedule_once` | Schedule a one-shot action at a specific time or after a delay |
| `schedule_cron` | Schedule a recurring action (cron expression or natural language) |
| `schedule_cancel` | Cancel a scheduled job |
| `schedule_list` | List all scheduled jobs |
| `schedule_status` | Get job status |

### Other

| Tool | Description |
|------|-------------|
| `remember` | Set a reminder that fires after a delay or at a specific time |
| `send_notification` | Send a notification through an output channel (when `channels:` configured) |

## Memory Tools (when memory module is active)

These tools are available when `modules.memory` is configured. They give the agent direct control over its cognitive state. See [Memory](05-memory.md) for full documentation.

### Working Memory

| Tool | Description |
|------|-------------|
| `set_goal` | Set the main goal and optional sub-goals |
| `set_plan` | Set the step-by-step plan (auto-creates todos) |
| `update_plan_step` | Advance to the next step in the plan |

### Task Management

| Tool | Description |
|------|-------------|
| `add_todo` | Add a task to the todo list |
| `update_todo` | Update a todo status (pending, in_progress, done, blocked) |

### Notes (sticky reminders)

| Tool | Description |
|------|-------------|
| `note` | Add a sticky note (something to not forget before finishing) |
| `resolve_note` | Mark a note as resolved |

### Knowledge

| Tool | Description |
|------|-------------|
| `add_fact` | Store an important fact (survives compaction) |
| `recall` | Search memory for relevant facts and context |
| `forget` | Remove a specific fact |

### Other Memory Tools

| Tool | Description |
|------|-------------|
| `track_entity` | Track an active entity (file, person, concept) |
| `add_relationship` | Add a relationship between entities |
| `checkpoint` | Save a self-assessment of progress |
| `cache_content` | Cache file content for O(1) access |
| `get_snapshot` | Get the full memory state |
| `add_episode` | Record a session episode |

## Agent Spawn Tools (when coordinator is configured)

These tools are available to the coordinator agent when specialists are defined in `agents:`. See [Multi-Agent Systems](12-multi-agent.md) for full documentation.

| Tool | Description |
|------|-------------|
| `Agent` | Spawn a sub-agent. Synchronous by default (`wait=true`). For parallel execution, call multiple `Agent` tools in the same turn. Set `wait=false` for background mode. |
| `AgentWaitAll` | Collect results from background agents (`wait=false`). Omit `agent_ids` to wait for all. |

The following actions exist internally but are **not exposed to the LLM**: `agent_status`, `agent_result`, `agent_list`, `agent_wait`, `agent_cancel`, `reassign_agent`. They can be used programmatically via hooks or middleware.

## Tool Injection Modes

How system tools are presented to the agent depends on the tool injection mode:

| Mode | When | System tools | Module tools |
|------|------|-------------|--------------|
| **Direct** | Small toolsets (< 20% of context) | Injected as direct function schemas | Also direct |
| **Discovery** | Large toolsets | Injected as direct function schemas | Behind meta-tools (search_tools, execute_tool) |

In both modes, system tools are **always directly callable** -- the agent never needs to "discover" them. Only domain tools (filesystem, database, http, etc.) are behind the discovery layer in discovery mode.
