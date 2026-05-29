---
id: agent_spawn
title: agent_spawn Module
sidebar_label: agent_spawn
sidebar_position: 6
description: One Agent tool, eight modes - spawn, wait, status, cancel, reassign, list, multi-wait.
---

# agent_spawn

Dynamic sub-agent creation and management. **One action,
eight modes.** The LLM sees a single `Agent` tool; the modes
are dispatched from params.

| Property | Value |
|----------|-------|
| Module id | `agent_spawn` |
| Action count | 1 (`agent_spawn.agent` → tool name `Agent`) |
| Type | shared (per-app), per-session task tracking |
| Permissions | `agent.spawn`, `agent.monitor`, `agent.control` |

## Design notes

- **One tool, eight modes** - the LLM mostly calls
  `Agent(prompt=...)`; coordinators use the hidden params for
  monitoring + control.
- **Background by default** - `wait=false` is the default;
  multiple `Agent` calls in one turn run concurrently via
  `asyncio.gather` (the action is in `_READ_ONLY_ACTIONS`).
- **Module sharing** - `memory`, `web`, `lsp`, `filesystem`,
  `shell` modules are shared with sub-agents (same instance,
  same cwd, same `_read_files` set, same memory store).
  Other modules get fresh instances. (.)
- **Universal directives** - the runner injects a mandatory
  prefix before every specialist's system prompt: *"Be FAST,
  no filler, go straight to tool calls, return only key
  findings."* Sub-agents never create tasks or set goals.
- **Cancellation propagation** - aborting the parent session
  cancels every running sub-agent and emits an `agent_cancel`
  event per agent.

## The single `Agent` action - 8 modes

Visible params:

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `prompt` | string | `null` | The task. Must be **self-contained** - the sub-agent cannot see the parent's conversation. |
| `description` | string | `""` | Short label shown in the UI (e.g. "Search API endpoints"). |
| `wait` | bool | `false` | Block until the agent finishes. |
| `specialist` | string \| null | `null` | Predefined agent id from `agents[]` (e.g. `web_researcher`, `writer`). Required so the coordinator can dispatch to the right specialist instead of a generic worker. |

Hidden params:

| Param | Type | Description |
|-------|------|-------------|
| `agent_id` | string | Reference an existing agent - check status, wait, cancel, reassign. |
| `agent_ids` | list[string] | Wait for multiple agents. Empty list = wait for all running. |
| `cancel` | bool | Cancel a running agent (requires `agent_id`). |
| `reassign` | string | New task for a failed / cancelled agent (requires `agent_id`). |
| `list_agents` | bool | List all agents with their status. |
| `system_prompt` | string | Custom system prompt for ad-hoc agents. |
| `max_turns` | int | Default `100`, max `10000`. |
| `timeout` | float | Default `3600.0` s, max `7200` s. |

### Mode 1 - Spawn background (default)

```python
Agent(prompt="Find all API endpoints in the repo")
# returns: {agent_id, status: "running", started_at}
```

Multiple parallel calls in one turn execute via
`asyncio.gather`.

### Mode 2 - Spawn and wait

```
Agent(prompt="Summarize README.md", wait=true)
```

Blocks until completion, returns the agent's result.

### Mode 3 - Check status

```python
Agent(agent_id="abc123")
# returns: {agent_id, status, duration_seconds, tool_calls_count, preview}
```

`status`: `running` / `completed` / `failed` / `cancelled`.

### Mode 4 - Wait for one

```
Agent(agent_id="abc123", wait=true)
```

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

Cancels the asyncio task and emits `agent_cancel`.

### Mode 7 - Reassign

```
Agent(agent_id="abc123", reassign="Try a different approach: ...")
```

Respawns a failed / cancelled agent with a new task.

### Mode 8 - List

```
Agent(list_agents=true)
```

Returns all agents for the current session with their
status.

## Specialist agents

A `specialist:` name selects a preconfigured system prompt +
tool allow-list. Declared under `agents:` with
`role: specialist` (or any role other than `coordinator`).

```yaml
agents:
  - id: explore
    role: specialist
    brain: { ... }
    modules:
      - {filesystem: [read, grep, glob]}    # only these 3 actions
      - {shell: [bash]}                      # full module
      - {memory: [remember]}                 # single action
    system_prompt: |
      You are an exploration specialist. Find and summarise code.
```

The `modules:` list supports two formats:

- `modules: [filesystem, shell]` - full access.
- `modules: [{filesystem: [read, grep, glob]}]` - restrict to
  specific actions.

Parsed →
`action_filter` dict → passed to
`build_index(action_filter=...)`. The LLM schema then
contains **only** the allowed tools.

## Pool configuration (coordinators)

```yaml
agents:
  - id: coordinator
    role: coordinator
    brain: { ... }
    pool:
      max_workers: 5         # max concurrent sub-agents [1, 100], default 3
      progress: true         # relay specialist progress events back, default false
      auto_retry: 1          # automatic retries on transient failures [0, 5], default 0
```

When the pool is full, additional `Agent` calls wait until
a slot frees up.

## Socket.IO events

Sub-agent lifecycle is streamed to the client via
`agent_event` (emitted by `_notify_bg` → `_relay` in
).

| Event | Fields | When |
|-------|--------|------|
| `spawn_agent` | `agent_id`, `specialist`, `task` | Agent launched. |
| `agent_progress` | `agent_id`, `duration_seconds`, `tool_calls_count`, `preview` | Mid-run heartbeat. |
| `agent_result` | `agent_id`, `result_summary`, `error?` | Completed or failed. |
| `agent_cancel` | `agent_id`, `reason`, `duration_seconds` | Cancelled (manual or session abort). |

## Session cleanup

`cleanup_session(session_id)` runs automatically on session
abort or end:

1. Cancels every pending asyncio task for running agents.
2. Emits `agent_cancel` events per agent (`reason:
   session_aborted`).
3. Orphaned tool calls in the parent session receive
   synthetic `"interrupted": true` results on resume.

Wired into the abort flow at
`abort_session` - kills the agent turn,
shell tasks, sub-agents, watchers; injects synthetic
`interrupted: true` results for orphaned tool calls on
session resume.

## Cross-references

- App-config block reference (`agents` + `agents[].pool`):
  [Agents](../../language/03-agents.md)
- Multi-agent example:
  [Examples → Multi-Agent App](../../language/15-examples.md#8--multi-agent-coordinator--worker)
- Memory module shared with sub-agents:
  [memory reference](memory.md)
