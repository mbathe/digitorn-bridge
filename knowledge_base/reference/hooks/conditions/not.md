---
id: hook-condition-not
title: "Hook condition: not"
type: hook-condition
condition: not
keywords: [not, condition, hook]
---

# Hook condition: `not`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_condition("not")`.

## Params
| Param | Requirement |
|-------|-------------|
| `condition` | required |

## Behavior
Logical NOT of a single inner condition.

YAML::

    condition:
      type: not
      condition: {type: tool_name, match: "memory.*"}

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": tool_start     # any hook event
    condition:
      type: not
      # params: condition
    action:
      type: log
      message: "not fired"
```
