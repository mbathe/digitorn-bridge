---
id: yaml-schema-behaviorcustomrule
title: "BehaviorCustomRule - YAML schema reference"
type: schema-reference
model: BehaviorCustomRule
is_root: false
keywords: [behaviorcustomrule, action, condition, enforce, id, message, rule, trigger]
---

# BehaviorCustomRule

## Description
Legacy custom rule format. Kept for backward compatibility.
Prefer ``rule_definitions`` for new apps.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `id` | str |  | `'custom'` |  |
| `rule` | str | ✓ | - |  |
| `enforce` | str |  | `'pre_tool'` |  |
| `trigger` | str |  | `''` |  |
| `condition` | dict[str, any] |  | `{}` |  |
| `action` | str |  | `'warn'` |  |
| `message` | str |  | `''` |  |

## Strictness
- `extra: forbid` - unknown keys cause a validation error
