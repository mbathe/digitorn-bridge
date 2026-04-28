---
id: yaml-schema-hookconditionconfig
title: "HookConditionConfig - YAML schema reference"
type: schema-reference
model: HookConditionConfig
is_root: false
keywords: [hookconditionconfig, type]
---

# HookConditionConfig

## Description
Condition configuration for an internal hook.

Built-in conditions:
- ``context_pressure``: fires when token usage exceeds threshold
- ``turn_count``: fires at a specific turn number or every N turns
- ``tool_calls``: fires when tool call count exceeds threshold
- ``message_count``: fires when message count exceeds threshold
- ``always``: fires every time (useful with cooldown)

Example::

condition:
type: context_pressure
threshold: 0.75
max_tokens: 128000

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `type` | str | ✓ | - | Condition type (registered name). |

## Strictness
- `extra: allow` - unknown keys are tolerated
