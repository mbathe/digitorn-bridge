---
id: yaml-schema-hookconfig
title: "HookConfig — YAML schema reference"
type: schema-reference
model: HookConfig
is_root: false
keywords: [hookconfig, action, condition, cooldown, enabled, id, max_fires, on, priority, tags]
---

# HookConfig

## Description
An internal hook: condition → action, evaluated during the agent loop.

Example::

hooks:
- id: context_compaction
"on": turn_end
condition:
type: context_pressure
threshold: 0.75
action:
type: compact_context
strategy: summarize
keep_last: 10
cooldown: 30

IMPORTANT: YAML 1.1 parses unquoted ``on`` as boolean ``True``.
Always quote it: ``"on": tool_end``. This schema rejects any
non-string value on that field.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `id` | str | ✓ | — | Unique hook identifier. |
| `on` | str |  | `'turn_end'` | When to evaluate. One of: activation, agent_complete, agent_spawn, approval_request, error, post_tool_use, pre_compact, pre_tool_use, session_end, session_start, tool_end, tool_start, turn_end, turn_start, user_prompt. MUST be quoted in YAML ('on' is a YAML 1.1 boolean keyword). |
| `condition` | [HookConditionConfig](HookConditionConfig.md) | ✓ | — | Condition that must be true for the hook to fire. |
| `action` | [HookActionConfig](HookActionConfig.md) | ✓ | — | Action to execute when the condition is met. |
| `cooldown` | float |  | `0.0` | Minimum seconds between fires (0 = no cooldown). |
| `max_fires` | int |  | `0` | Max times this hook can fire per app lifetime. 0 = unlimited. Useful for one-shot setup hooks or for bounding runaway triggers. |
| `priority` | int |  | `100` | Evaluation order among hooks on the same event. Lower runs first. Same priority → YAML order is preserved. Default 100. |
| `enabled` | bool |  | `True` | Feature flag. When False the hook is loaded but never fires — lets apps A/B gate new behavior without YAML surgery. |
| `tags` | list[str] |  | `[]` | Free-form tags for grouping / querying hooks. Not used by the runtime — surfaced in /api/apps/{id}/hooks for introspection. |

## Linked models
- [HookActionConfig](HookActionConfig.md)
- [HookConditionConfig](HookConditionConfig.md)

## Strictness
- `extra: forbid` — unknown keys cause a validation error
