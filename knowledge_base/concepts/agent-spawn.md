---
id: agent-spawn
title: "Agent Spawn (multi-agent coordination)"
type: concept
keywords: [agent_spawn, multi_agent, coordinator, specialist, spawn_agent, agent_wait, agent_wait_all, agent_result, agent_cancel, agent_list, fan_out, parallel, sub_agent, worker, pool]
related: [execution-modes, capabilities, brain-providers]
source: packages/digitorn/modules/agent_spawn/runner.py
---

# Agent Spawn -- multi-agent coordination

## What it is

The `agent_spawn` module enables a **coordinator agent** to spawn **specialist sub-agents** that run in parallel as independent asyncio tasks. Each sub-agent gets its own message history, tool set, and execution context. The coordinator orchestrates them using a single unified `Agent` tool with multiple operation modes controlled by parameters.

The canonical pattern is: **plan -> fan-out -> join -> synthesize**.

## How it works

1. The app YAML defines multiple agents with different roles, specialties, and optionally different LLM models.
2. The coordinator agent (role: coordinator) has access to `agent_spawn.agent`.
3. When the coordinator spawns a specialist, the runtime creates an isolated agent loop with the specialist's brain, system_prompt, and restricted module set.
4. Shared modules (`memory`, `web`, `filesystem`, `shell`) are the **same instance** -- sub-agents see the same workspace, cwd, read_files set, and memory store.
5. Other modules get fresh instances per sub-agent.

## YAML reference

### Defining agents

```yaml
agents:
  # The coordinator -- orchestrates but doesn't do the work
  - id: coordinator
    role: coordinator
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
      temperature: 0.2
      max_tokens: 4096
    system_prompt: |
      You are the coordinator. Decompose tasks and delegate to specialists.
    pool:
      max_workers: 5          # Max concurrent sub-agents

  # A specialist -- spawned on demand by the coordinator
  - id: web_researcher
    role: specialist
    specialty: "Search the web and extract facts"
    brain:
      provider: deepseek
      model: deepseek-chat
      temperature: 0.2
      max_tokens: 3072
    system_prompt: |
      You are a web researcher. Find facts on the topic given to you.
    modules: [web, memory]    # Restricted module access

  - id: writer
    role: specialist
    specialty: "Turn raw findings into a structured report"
    brain:
      provider: deepseek
      model: deepseek-chat
      temperature: 0.4
      max_tokens: 6144
    system_prompt: |
      You are a writer. Synthesize findings into a report.
    modules: [memory]
```

### Agent fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique agent identifier |
| `role` | string | no | `coordinator`, `specialist`, or `worker` (default: `worker`) |
| `brain` | AgentBrain | yes | LLM provider configuration |
| `system_prompt` | string | no | System prompt for this agent |
| `specialty` | string | no | Short description shown to coordinator |
| `skills` | string | no | Path to .md file with detailed instructions |
| `capabilities` | list | no | Skill names to auto-load from `skills/` folder |
| `modules` | list | no | Module IDs this specialist can access (empty = same as coordinator) |
| `pool` | dict | no | `{max_workers: N}` -- max concurrent sub-agents (coordinator only) |
| `plan_first` | bool | no | Force agent to explain plan before using tools (default: true) |

### Granting agent_spawn access

The coordinator must have `agent_spawn.agent` granted in capabilities:

```yaml
capabilities:
  default_policy: auto
  grant:
    - module: agent_spawn
      actions: [agent]
```

## The Agent tool -- one tool, multiple modes

The agent uses a single `Agent` tool. The operation mode is determined by which parameters are provided:

### Spawn a specialist (wait=true, blocking)

```
Agent(prompt="Research angle X", specialist="web_researcher")
```
Blocks until the sub-agent completes. Returns the result directly.

### Spawn a specialist (wait=false, non-blocking)

```
Agent(prompt="Research angle X", specialist="web_researcher", wait=false)
```
Returns immediately with an `agent_id`. Use `Agent(agent_ids=[...])` to collect results later.

### Wait for multiple agents

```
Agent(agent_ids=["id1", "id2", "id3"])
```
Blocks until all specified agents complete. Returns all results.

### Check agent status

```
Agent(agent_id="id1")
```
Returns the current status without blocking.

### Cancel an agent

```
Agent(agent_id="id1", cancel=true)
```
Cancels the running agent.

### List all agents

```
Agent(list=true)
```
Returns all tracked agents and their statuses.

## The fan-out / join pattern

This is the canonical multi-agent pattern:

```
1. Coordinator receives task
2. memory.set_goal("Research topic X")     # Shared with all workers
3. Agent(prompt="angle 1", specialist="web_researcher", wait=false) -> id1
4. Agent(prompt="angle 2", specialist="web_researcher", wait=false) -> id2
5. Agent(prompt="fact check", specialist="fact_checker", wait=false) -> id3
6. Agent(agent_ids=[id1, id2, id3])        # Wait for all, collect results
7. Combine findings
8. Agent(prompt="Write report from: <findings>", specialist="writer")  # Blocking
9. Agent(prompt="Polish: <draft>", specialist="editor")                # Blocking
10. Return final result
```

Key rules:
- Spawn ALL parallel agents BEFORE waiting for any of them
- Use `wait=false` for parallel work, `wait=true` (default) for sequential
- Sub-agents share memory -- `set_goal()` is visible to all workers via auto-injected memory snapshot
- If a worker fails, continue with the others -- partial results beat no results

## Module sharing

| Module | Shared? | Why |
|--------|---------|-----|
| `memory` | Yes | Workers see the coordinator's goal, todos, and remembered facts |
| `web` | Yes | Shared HTTP client and search state |
| `filesystem` | Yes | Same workspace, same cwd, same read_files set |
| `shell` | Yes | Same shell process state |
| `lsp` | Yes | Same language server connections |
| All others | No | Fresh instances per sub-agent |

## Per-agent model strategy

Different agents can use different models for cost optimization:

```yaml
agents:
  - id: coordinator
    brain:
      provider: anthropic
      model: claude-sonnet-4-20250514
      config:
        api_key: "claude-code"

  - id: fact_checker
    brain:
      provider: deepseek
      model: deepseek-chat        # Cheap, fast model
      temperature: 0.0

  - id: writer
    brain:
      provider: anthropic
      model: claude-sonnet-4-20250514     # Strong model for quality
      config:
        api_key: "claude-code"
      temperature: 0.4
```

## Examples

### Full multi-agent research team (template 05)

```yaml
app:
  app_id: research-team
  name: "Research Team"

modules:
  web:
    config:
      search:
        primary: duckduckgo
  memory:
    config:
      working_memory: true
      todo_list: true
  filesystem: {}

agents:
  - id: coordinator
    role: coordinator
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
      temperature: 0.2
      max_tokens: 4096
    system_prompt: |
      You are the research coordinator. Decompose the user's question
      into 2-4 angles, spawn researchers in parallel, wait for all,
      then delegate to the writer and editor.
    pool:
      max_workers: 5

  - id: web_researcher
    role: specialist
    specialty: "Search the web and extract relevant facts"
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
      temperature: 0.2
      max_tokens: 3072
    system_prompt: |
      You are a web researcher. Search for facts on your assigned angle.
      Return structured findings with citations.
    modules: [web, memory]

  - id: writer
    role: specialist
    specialty: "Synthesize findings into a structured report"
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
      temperature: 0.4
      max_tokens: 6144
    system_prompt: |
      You are a writer. Turn the findings into a structured report.
    modules: [memory]

execution:
  mode: one_shot
  entry_agent: coordinator
  max_turns: 25
  timeout: 600

capabilities:
  default_policy: auto
  grant:
    - module: web
      actions: [search, fetch, extract]
    - module: memory
      actions: [set_goal, remember, task_create, task_update]
    - module: filesystem
      actions: [read, write, glob]
    - module: agent_spawn
      actions: [agent]
```

### Coding assistant with explore/plan agents

```yaml
agents:
  - id: main
    role: coordinator
    brain: { ... }
    system_prompt: |
      Use Agent(specialist="explore") for codebase searches.
      Use Agent(specialist="plan") for architecture decisions.
    pool:
      max_workers: 3

  - id: explore
    role: specialist
    specialty: "Fast codebase exploration. Read-only."
    brain: { ... }
    modules: [filesystem, memory]

  - id: plan
    role: specialist
    specialty: "Architecture and implementation planning. Read-only."
    brain: { ... }
    modules: [filesystem, memory]

capabilities:
  grant:
    - module: agent_spawn
      actions: [agent]
```
