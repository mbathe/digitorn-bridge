---
id: hook-condition-all_of
title: "Hook condition: all_of"
type: hook-condition
condition: all_of
keywords: [all_of, condition, hook, conditions]
---

# Hook condition: `all_of`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_condition("all_of")`.

## Params
| Param | Requirement |
|-------|-------------|
| `conditions` | required |

## Behavior
Logical AND over a list of inner conditions. Short-circuits on
the first False. Empty list returns True.

YAML::

    condition:
      type: all_of
      conditions:
        - {type: tool_name, match: "filesystem.*"}
        - {type: tool_failed}
        - {type: turn_count, threshold: 5}

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": tool_start     # any hook event
    condition:
      type: all_of
      # params: conditions
    action:
      type: log
      message: "all_of fired"
```
