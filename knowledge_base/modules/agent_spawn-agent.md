---
id: agent_spawn-agent
title: "agent_spawn.agent (Agent)"
type: module-action
module: agent_spawn
action: agent
fqn: agent_spawn.agent
short_name: Agent
keywords: [agent_spawn, agent, multi-agent, spawn]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# agent_spawn.agent (Agent)

## Description
Launch a sub-agent to work on a task.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `prompt` | string |  | — | The task for the agent. Must be self-contained — the agent cannot see your conversation. |
| `description` | string |  | `` | Short label for the UI (e.g. 'Search API endpoints'). |
| `agent_id` | string |  | — | Existing agent ID — check status, wait, cancel, or reassign. |
| `agent_ids` | array |  | — | Wait for multiple agents. Omit = wait for all running. |
| `wait` | boolean |  | `False` | Wait for the agent to finish (blocks until done). Default: false (background). |
| `cancel` | boolean |  | `False` | Cancel a running agent (requires agent_id). |
| `reassign` | string |  | — | New task for a failed/cancelled agent (requires agent_id). |
| `list_agents` | boolean |  | `False` | List all agents with their status. |
| `specialist` | string |  | — | Optional specialist agent id to run under (e.g. 'web_researcher', 'writer', 'explore'). Must match one of the ``agents:`` declared in the app YAML. Omit for the default general-purpose worker. |
| `system_prompt` | string |  | — | Custom system prompt for ad-hoc agents. |
| `max_turns` | integer |  | `100` | Maximum turns before the agent stops. |
| `timeout` | number |  | `3600.0` | Max execution time in seconds. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: agent_spawn
      actions: [agent]
```

## Tool usage instructions
```
Launch an isolated sub-agent with its own context window.
The agent shares your workspace, filesystem, shell, and memory — but cannot see your conversation.

## Default: background (non-blocking)

Agents run in background by default. You get an agent_id back instantly.
Launch multiple agents in one turn — they all run concurrently:
  Agent(prompt='Search auth code for vulnerabilities')
  Agent(prompt='Search database code for SQL injection')
  Agent(prompt='Search API routes for missing validation')
Then collect all results:
  Agent(agent_ids=['agent_abc', 'agent_def', 'agent_ghi'])

## Blocking mode (wait=true)

Use wait=true when you need the result immediately before continuing:
  Agent(prompt='Read src/auth.py and explain the OAuth flow', wait=true)

## Other modes

Status:   Agent(agent_id='agent_abc')               → check progress
Cancel:   Agent(agent_id='agent_abc', cancel=true)   → stop it
Collect:  Agent(agent_ids=['id1', 'id2'])            → wait for multiple
Reassign: Agent(agent_id='agent_abc', reassign='Try differently: ...')
List:     Agent(list=true)

## Prompt writing rules

The agent starts with ZERO context. Your prompt is everything it knows.

Always include:
- What to do and why (goal + motivation)
- File paths, line numbers, error messages, function names
- What you already know or ruled out
- Whether to write code or just research

Bad:  Agent(prompt='fix the bug')
Good: Agent(prompt='parse_config() in src/config.py:42 raises KeyError on empty YAML. Read the function, fix it, run pytest tests/test_config.py.')

Never delegate understanding — gather info, synthesize it yourself, then delegate the specific action with full context.
```

## Safety
- Risk level: **medium**
