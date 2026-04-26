---
id: hook-condition-tool_calls
title: "Hook condition: tool_calls"
type: hook-condition
condition: tool_calls
keywords: [tool_calls, condition, hook, threshold]
---

# Hook condition: `tool_calls`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_condition("tool_calls")`.

## Params
| Param | Requirement |
|-------|-------------|
| `threshold` | required |

## Behavior
Fire when tool call count exceeds a threshold.

Params:
    threshold (int): Tool call count to trigger. Default: 20

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": tool_start     # any hook event
    condition:
      type: tool_calls
      # params: threshold
    action:
      type: log
      message: "tool_calls fired"
```
