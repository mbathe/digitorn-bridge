---
id: yaml-schema-hookactionconfig
title: "HookActionConfig - YAML schema reference"
type: schema-reference
model: HookActionConfig
is_root: false
keywords: [hookactionconfig, type]
---

# HookActionConfig

## Description
Action configuration for an internal hook.

Built-in actions:
- ``compact_context``: intelligently compact message history
- ``inject_message``: inject a message into the conversation
- ``module_action``: call any module action
- ``log``: log a message (debugging)

Example::

action:
type: compact_context
strategy: summarize
keep_last: 10

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `type` | str | ✓ | - | Action type (registered name). |

## Strictness
- `extra: allow` - unknown keys are tolerated
