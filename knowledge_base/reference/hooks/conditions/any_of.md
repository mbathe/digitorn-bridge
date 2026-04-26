---
id: hook-condition-any_of
title: "Hook condition: any_of"
type: hook-condition
condition: any_of
keywords: [any_of, condition, hook, conditions]
---

# Hook condition: `any_of`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_condition("any_of")`.

## Params
| Param | Requirement |
|-------|-------------|
| `conditions` | required |

## Behavior
Logical OR over a list of inner conditions. Short-circuits on
the first True. Empty list returns False.

YAML::

    condition:
      type: any_of
      conditions:
        - {type: context_pressure, threshold: 0.9}
        - {type: tool_failed}

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": tool_start     # any hook event
    condition:
      type: any_of
      # params: conditions
    action:
      type: log
      message: "any_of fired"
```
