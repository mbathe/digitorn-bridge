---
id: hook-condition-tool_failed
title: "Hook condition: tool_failed"
type: hook-condition
condition: tool_failed
keywords: [tool_failed, condition, hook]
---

# Hook condition: `tool_failed`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_condition("tool_failed")`.

## Params
_(no params)_

## Behavior
Fire when the last tool execution failed.

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": tool_start     # any hook event
    condition:
      type: tool_failed
    action:
      type: log
      message: "tool_failed fired"
```
