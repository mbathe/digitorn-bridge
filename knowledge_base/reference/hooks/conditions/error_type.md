---
id: hook-condition-error_type
title: "Hook condition: error_type"
type: hook-condition
condition: error_type
keywords: [error_type, condition, hook, match]
---

# Hook condition: `error_type`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_condition("error_type")`.

## Params
| Param | Requirement |
|-------|-------------|
| `match` | required |

## Behavior
Fire when a specific error type occurs.

Params:
    match (str): Error code pattern. E.g. "rate_limited", "auth_*"

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": tool_start     # any hook event
    condition:
      type: error_type
      # params: match
    action:
      type: log
      message: "error_type fired"
```
