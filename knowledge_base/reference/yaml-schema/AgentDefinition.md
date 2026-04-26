---
id: yaml-schema-agentdefinition
title: "AgentDefinition — YAML schema reference"
type: schema-reference
model: AgentDefinition
is_root: false
keywords: [agentdefinition, brain, capabilities, delegate_to, hooks, id, modules, plan_first, pool, role, skills]
---

# AgentDefinition

## Description
Definition of a single agent in the app YAML.

Only ``id`` and ``brain`` are required for now.
Other fields (tools, signals, loop, watch) will be added
when we implement the full agent runtime.

Example::

agents:
- id: coordinator
role: coordinator
brain:
provider: deepseek
model: deepseek-chat
temperature: 0.2
config:
api_key: "{{secret.DEEPSEEK_API_KEY}}"
base_url: "https://api.deepseek.com/v1"
system_prompt: |
You are a coordinator agent.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `id` | str | ✓ | — | Unique agent identifier within this app. |
| `role` | str |  | `'worker'` | Agent role hint. Functional roles: 'coordinator' (can spawn agents), 'specialist' (pre-configured expert), 'worker' (default). Descriptive roles like 'assistant', 'analyst', 'reviewer' are also accepted and used in the system prompt. |
| `brain` | [AgentBrain](AgentBrain.md) | ✓ | — | LLM provider configuration for this agent. |
| `system_prompt` | str |  | `''` | System prompt injected at conversation start. |
| `plan_first` | bool |  | `True` | When true, the agent must explain its plan in plain text before executing any tools on the first turn. Prevents silent tool calls. |
| `specialty` | str |  | `''` | Short description of this specialist's expertise (shown to coordinator). |
| `delegate_to` | list[str] |  | `[]` | Agent IDs this coordinator can delegate to. The compiler verifies each entry references a declared agent id. |
| `skills` | str |  | `''` | Path to a .md file with detailed methodology/instructions for this specialist. |
| `capabilities` | list[str] |  | `[]` | List of skill names to auto-load from the bundle's ``skills/`` directory. The compiler reads ``skills/<name>.md`` for each entry and appends the content to this agent's ``system_prompt`` under an ``## Available capabilities`` section. Clean way to separate the agent's identity (system_prompt) from its skill definitions (individual markdown files). |
| `modules` | list[any] |  | `[]` | Modules this specialist can access. Empty = same as coordinator. Supports two formats:   - Simple: ['filesystem', 'shell', 'memory'] — full module access   - Granular: [{'filesystem': ['read', 'grep', 'glob']}, 'shell', 'memory'] — restrict actions per module |
| `pool` | dict[str, any] |  | `{}` | Agent pool config for coordinators. Keys: max_workers (int). |
| `hooks` | list[[HookConfig](HookConfig.md)] |  | `[]` | Per-agent hooks — merged with ``execution.hooks`` but only evaluated when this specific agent is active. Use for specialist-specific behavior (e.g. a `reviewer` agent that runs extra lint, a `writer` agent that logs every edit). App-wide hooks still fire for every agent; these add on top. |

## Linked models
- [AgentBrain](AgentBrain.md)
- [HookConfig](HookConfig.md)

## Strictness
- `extra: forbid` — unknown keys cause a validation error
