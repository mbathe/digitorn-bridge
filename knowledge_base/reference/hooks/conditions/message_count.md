---
id: hook-condition-message_count
title: "Hook condition: message_count"
type: hook-condition
condition: message_count
keywords: [message_count, condition, hook, threshold]
---

# Hook condition: `message_count`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_condition("message_count")`.

## Params
| Param | Requirement |
|-------|-------------|
| `threshold` | required |

## Behavior
Fire when message count exceeds a threshold.

Params:
    threshold (int): Message count. Default: 50

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": tool_start     # any hook event
    condition:
      type: message_count
      # params: threshold
    action:
      type: log
      message: "message_count fired"
```
