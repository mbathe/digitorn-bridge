---
id: module-concept-agent_spawn
title: "agent_spawn module - overview"
type: module-concept
module: agent_spawn
isolation: shared
keywords: [agent_spawn, agent_spawn-module, agent]
version: 2.0.0
---

# `agent_spawn` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `2.0.0`
- **Actions**: 1 visible, 0 internal

## Description (from class docstring)

Agent Spawn Module - 1 ultra-powerful Agent tool with mode dispatch.

Single tool, 8 modes (like Shell):
  1. Spawn sync:   Agent(prompt='...')                    → run, wait, return result
  2. Spawn async:  Agent(prompt='...', wait=false)        → launch background, return agent_id
  3. Status:       Agent(agent_id='...')                   → check agent status
  4. Wait one:     Agent(agent_id='...', wait=true)        → block until done
  5. Wait all:     Agent(agent_ids=[...])                  → wait for multiple agents
  6. Cancel:       Agent(agent_id='...', cancel=true)      → terminate agent
  7. Reassign:     Agent(agent_id='...', reassign='task')  → respawn failed agent
  8. List:         Agent(list=true)                        → list all agents

> Class-level summary: Multi-agent orchestration - 1 tool, 8 modes.

## Configuration

Set under `modules.agent_spawn.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon. |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `agent` | `Agent` |  | medium | Launch a sub-agent to work on a task. |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: agent_spawn
      actions: [agent]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {agent_spawn: [agent]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/agent_spawn-*.md`.
