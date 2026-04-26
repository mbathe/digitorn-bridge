---
id: yaml-schema-classifiercontextconfig
title: "ClassifierContextConfig — YAML schema reference"
type: schema-reference
model: ClassifierContextConfig
is_root: false
keywords: [classifiercontextconfig, history_depth, recent_history, session_state, tool_inventory, workspace_info]
---

# ClassifierContextConfig

## Description
What context the classifier receives about the agent's state.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `tool_inventory` | bool |  | `True` | Send the agent's tool names + descriptions. |
| `session_state` | bool |  | `True` | Send session state: files read/edited, searches, violations, turn number. |
| `workspace_info` | bool |  | `True` | Send workspace metadata: project type, languages, file count. |
| `recent_history` | bool |  | `True` | Send recent messages with tool calls and results. |
| `history_depth` | int |  | `8` | How many recent messages to include. |

## Strictness
- `extra: forbid` — unknown keys cause a validation error
