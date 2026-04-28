---
id: agent_spawn
title: Agent Spawn Module
sidebar_label: agent_spawn
sidebar_position: 6
description: 1 unified Agent tool with 8 modes - spawn, wait, status, cancel, reassign, list, multi-wait.
---

# agent_spawn

Dynamic sub-agent creation and management. **One action, eight modes.** The LLM sees a single `Agent` tool; modes are dispatched from params.

| Property | Value |
|----------|-------|
| **Module ID** | `agent_spawn` |
| **Isolation** | shared (per-app); agents tracked per-session |
| **Platforms** | All |
| **Permissions** | `agent.spawn`, `agent.monitor`, `agent.control` |

---

## Design Philosophy

- **One tool, eight modes** - LLMs call `Agent(prompt=...)` most of the time; sophisticated coordinators use the hidden params for monitoring and control.
- **Background by default** - `wait=false` is the default, so multiple `Agent()` calls in a single turn run concurrently via `asyncio.gather`.
- **Module sharing** - `memory`, `web`, `lsp`, `filesystem`, `shell` modules are shared with sub-agents (same instance + cwd + read-files + memory store). Other modules get fresh instances.
- **Universal directives** - the runner injects a mandatory prefix: "Be FAST, no filler, go straight to tool calls, return only key findings." Sub-agents never create tasks or set goals.
- **Cancellation propagation** - aborting the parent session cancels all running sub-agents and emits `agent_cancel` events per agent.

---

## The `Agent` action - 8 modes

| Tool Name | Action |
|-----------|--------|
| `Agent` | `agent_spawn.agent` |

**Visible params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `prompt` | string | null | The task. Must be self-contained - the sub-agent cannot see the parent's conversation. |
| `description` | string | `""` | Short label shown in UI (e.g. "Search API endpoints"). |
| `wait` | bool | `false` | Block until the agent finishes. Default: false (background). |

**Hidden params:**

| Param | Type | Description |
|-------|------|-------------|
| `agent_id` | string | Reference an existing agent - check status, wait, cancel, or reassign. |
| `agent_ids` | list[string] | Wait for multiple agents. Omit to wait for all running. |
| `cancel` | bool | Cancel a running agent (requires `agent_id`). |
| `reassign` | string | New task for a failed/cancelled agent (requires `agent_id`). |
| `list_agents` | bool | List all agents with their status. |
| `specialist` | string | Predefined agent type: `explore`, `plan`, `worker`, `verification`. |
| `system_prompt` | string | Custom system prompt for ad-hoc agents. |
| `max_turns` | int | Default 100, max 10 000. |
| `timeout` | float | Default 1 800 s, max 7 200 s. |

---

### Mode 1 - Spawn background

```
Agent(prompt="Find all API endpoints in the repo")
```

Launches the agent and returns `{agent_id, status: "running", started_at}` immediately. The agent is an entry in `_READ_ONLY_ACTIONS`, so multiple `Agent()` calls in one turn execute concurrently.

### Mode 2 - Spawn and wait

```
Agent(prompt="Summarize README.md", wait=true)
```

Blocks until completion, returns the agent's result.

### Mode 3 - Check status

```
Agent(agent_id="abc123")
```

Returns `{agent_id, status, duration_seconds, tool_calls_count, preview}`. Status is `running`, `completed`, `failed`, or `cancelled`.

### Mode 4 - Wait for one

```
Agent(agent_id="abc123", wait=true)
```

Blocks until the specified agent finishes.

### Mode 5 - Wait for many

```
Agent(agent_ids=["abc123", "def456"])    # specific set
Agent(agent_ids=[])                       # all currently running
```

Returns results in the same order as `agent_ids`.

### Mode 6 - Cancel

```
Agent(agent_id="abc123", cancel=true)
```

Cancels the agent's asyncio task and emits an `agent_cancel` event.

### Mode 7 - Reassign

```
Agent(agent_id="abc123", reassign="Try a different approach: ...")
```

Respawns a failed/cancelled agent with a new task.

### Mode 8 - List

```
Agent(list_agents=true)
```

Returns all agents for the current session with their status.

---

## Specialist types

A `specialist:` name selects a preconfigured system prompt + tool allow-list. Declared under `agents:` with `role: specialist`.

```yaml
agents:
  - id: explore
    role: specialist
    brain: { ... }
    modules:
      - {filesystem: [read, grep, glob]}   # granular: only these 3 actions
      - {shell: [bash]}                    # full module
      - {memory: [remember]}               # single action
    system_prompt: |
      You are an exploration specialist. Find and summarize code.
```
The YAML `modules:` list supports two formats:

- `modules: [filesystem, shell]` - full access
- `modules: [{filesystem: [read, grep, glob]}]` - restrict to specific actions

Parsed in `bootstrap.py::_register_specialist()` → `action_filter` dict → passed to `build_index(action_filter=...)`. The LLM schema then contains ONLY the allowed tools.

---

## Events (SSE)

Sub-agent lifecycle is streamed to the frontend via `agent_event`:

| Event | Fields | When |
|-------|--------|------|
| `spawn_agent` | `agent_id`, `specialist`, `task` | Agent launched |
| `agent_progress` | `agent_id`, `duration_seconds`, `tool_calls_count`, `preview` | Mid-run heartbeat |
| `agent_result` | `agent_id`, `result_summary`, `error` (if any) | Completed or failed |
| `agent_cancel` | `agent_id`, `reason`, `duration_seconds` | Cancelled (manual or session abort) |

Implementation: `_notify_bg` → `_relay` in `modules/agent_spawn/manager.py`.

---

## Pool configuration (coordinator agents)

```yaml
agents:
  - id: coordinator
    role: coordinator
    brain: { ... }
    pool:
      max_workers: 5       # max concurrent sub-agents this coordinator can spawn
      queue_size: 20       # pending tasks allowed before backpressure
```
When the pool is full, additional `Agent()` calls wait until a slot frees up.

---

## Session cleanup

`cleanup_session(session_id)` is called automatically on session abort or end:

1. Cancels every pending asyncio task for running agents.
2. Emits `agent_cancel` events per agent (reason: `session_aborted`).
3. Orphaned tool calls in the parent session receive synthetic `"interrupted": true` results on resume.
