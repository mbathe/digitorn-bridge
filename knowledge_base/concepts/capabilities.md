---
id: capabilities
title: "Capabilities (security and access control)"
type: concept
keywords: [capabilities, default_policy, max_risk_level, grant, approve, deny, hidden_modules, hidden_actions, approval_timeout, security, permissions, access_control]
related: [execution-modes, agent-spawn, channels]
source: packages/digitorn/core/app/schema.py
---

# Capabilities -- security and access control

## What it is

The `capabilities:` block controls which tools (module actions) the agent can access and how. It implements a **deny-by-default** security model where every action must be explicitly granted, approved, or blocked.

When `capabilities` is absent from the YAML, no security enforcement is applied (dev/test mode). In production, always include it.

## YAML reference

```yaml
capabilities:
  default_policy: block        # auto, approve, or block
  max_risk_level: medium       # low, medium, or high
  approval_timeout: 300        # Seconds to wait for user approval (30-3600)

  grant:                       # Actions the agent can call freely
    - module: filesystem
      actions: [read, grep, glob]
    - module: memory
      actions: [set_goal, remember, task_create, task_update]

  approve:                     # Actions requiring user approval
    - module: filesystem
      actions: [write, edit]
    - module: shell
      actions: [bash]

  deny:                        # Actions the agent cannot call at all
    - module: database
      actions: [execute_query]
      reason: "Read-only mode"

  hidden_modules:              # Modules invisible to the agent
    - preview                  # Loaded but agent can't see or call

  hidden_actions:              # Specific actions invisible to the agent
    - module: filesystem
      actions: [rm, mv, cp]
```

## Policy resolution order

When the agent calls a tool, the system resolves permissions in this order (first match wins):

1. **Action-level override** -- explicit grant/approve/deny for that specific action
2. **Risk-based rules** -- max_risk_level check against the action's declared risk
3. **Module-level default** -- grant for the entire module (actions: [] = all)
4. **App-level default_policy** -- the fallback

## default_policy

| Value | Behavior |
|-------|----------|
| `auto` | All non-denied actions are allowed without approval |
| `approve` | All non-granted/denied actions require user approval |
| `block` | All non-granted actions are blocked (most restrictive) |

## max_risk_level

Each module action has a declared risk level. The `max_risk_level` caps what the agent can access:

| Level | Allowed actions |
|-------|----------------|
| `low` | Only safe, read-only actions |
| `medium` | Read + write actions, no destructive ops |
| `high` | Everything, including destructive actions |

## grant -- free access

Actions listed under `grant` can be called by the agent without any approval. This is the most common block.

```yaml
grant:
  - module: filesystem
    actions: [read, write, edit, grep, glob]  # Specific actions
  - module: memory                             # Empty = ALL actions
  - module: web
    actions: [search, fetch, extract]
```

When `actions` is empty or omitted, ALL actions on the module are granted.

## approve -- requires user confirmation

Actions under `approve` are available to the agent but require explicit user approval before each execution. The runtime pauses and shows an approval prompt.

```yaml
approve:
  - module: filesystem
    actions: [write, edit]
  - module: shell
    actions: [bash]
```

`approval_timeout` controls how long to wait before auto-denying (default: 300 seconds, range: 30-3600).

## deny -- blocked entirely

Actions under `deny` cannot be called at all. The agent receives an error if it tries. Use `reason` to explain why.

```yaml
deny:
  - module: database
    actions: [execute_query]
    reason: "Read-only mode — only SELECT queries allowed"
  - module: filesystem
    actions: [rm]
    reason: "File deletion is disabled for safety"
```

## hidden_modules

Modules listed here are **loaded and functional** but **invisible to the agent**. The agent cannot see them in its tool index and cannot call their actions.

Hidden modules are still usable by:
- Setup steps
- Hooks
- Channel activation pipelines
- Other modules internally

```yaml
hidden_modules:
  - preview       # SSE transport -- internal only
  - lsp           # Language server -- called by hooks, not by agent
```

## hidden_actions

Like `hidden_modules` but at the action level. Individual actions are removed from the agent's tool index but remain callable by hooks, setup, and other internal mechanisms.

```yaml
hidden_actions:
  - module: filesystem
    actions: [rm, mv, cp]      # Hide destructive file ops
  - module: shell
    actions: [bash_background]  # Hide background shell
```

The difference between `deny` and `hidden_actions`:
- **deny**: the agent sees the action exists but gets an error when calling it
- **hidden_actions**: the agent doesn't know the action exists at all

## Granting agent_spawn

For multi-agent apps, the coordinator needs `agent_spawn.agent` granted:

```yaml
capabilities:
  grant:
    - module: agent_spawn
      actions: [agent]
```

## Granting ask_user

To give the agent structured question capabilities (multiple choice, forms):

```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [ask_user]
```

## Examples

### Restrictive -- read-only coding assistant

```yaml
capabilities:
  default_policy: block
  max_risk_level: low
  grant:
    - module: filesystem
      actions: [read, grep, glob]
    - module: memory
      actions: [set_goal, remember]
  deny:
    - module: filesystem
      actions: [write, edit, rm]
      reason: "Read-only mode"
    - module: shell
      reason: "Shell access disabled"
```

### Moderate -- standard coding assistant

```yaml
capabilities:
  default_policy: block
  max_risk_level: medium
  grant:
    - module: filesystem
      actions: [read, grep, glob]
    - module: memory
      actions: [set_goal, remember, task_create, task_update]
    - module: web
      actions: [search, fetch, extract]
    - module: agent_spawn
      actions: [agent]
  approve:
    - module: filesystem
      actions: [write, edit]
    - module: shell
      actions: [bash]
```

### Permissive -- full access automation

```yaml
capabilities:
  default_policy: auto
  max_risk_level: high
  grant:
    - module: filesystem
      actions: [read, write, edit, grep, glob]
    - module: shell
      actions: [bash]
    - module: web
      actions: [search, fetch, extract, download]
    - module: memory
      actions: [set_goal, remember, task_create, task_update]
    - module: agent_spawn
      actions: [agent]
    - module: database
      actions: [fetch_results, execute_query]
```

### Background app with channels

```yaml
capabilities:
  default_policy: block
  max_risk_level: medium
  grant:
    - module: web
      actions: [search, fetch]
    - module: memory
      actions: [set_goal, remember]
    - module: channels
      actions: [send_message, reply]
  hidden_modules:
    - preview
```

### Builder with workspace

```yaml
capabilities:
  default_policy: block
  max_risk_level: medium
  grant:
    - module: rag
      actions: [query, multi_query, list_knowledge_bases]
    - module: http
      actions: [get, post, json_api]
    - module: memory
      actions: [set_goal, remember, task_create, task_update]
    - module: context_builder
      actions: [ask_user]
    - module: workspace
      actions: [write, read, edit, glob, grep, delete]
    - module: shell
      actions: [bash]
```
