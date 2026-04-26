---
id: hook-condition-turn_count
title: "Hook condition: turn_count"
type: hook-condition
condition: turn_count
keywords: [turn_count, condition, hook, threshold, every]
---

# Hook condition: `turn_count`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_condition("turn_count")`.

## Params
| Param | Requirement |
|-------|-------------|
| `every` | optional |
| `threshold` | required |

## Behavior
Fire when the turn count reaches a threshold.

Params:
    threshold (int): Turn number to fire at. Default: 10
    every (int): Fire every N turns. Default: 0 (disabled)

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": tool_start     # any hook event
    condition:
      type: turn_count
      # params: threshold, every
    action:
      type: log
      message: "turn_count fired"
```
