---
id: hook-condition-tool_name
title: "Hook condition: tool_name"
type: hook-condition
condition: tool_name
keywords: [tool_name, condition, hook, match]
---

# Hook condition: `tool_name`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_condition("tool_name")`.

## Params
| Param | Requirement |
|-------|-------------|
| `match` | required |

## Behavior
Fire when the current tool matches a pattern.

Params:
    match (str|list): Tool name(s) to match. Supports wildcards: "filesystem.*", "Write|Edit"

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": tool_start     # any hook event
    condition:
      type: tool_name
      # params: match
    action:
      type: log
      message: "tool_name fired"
```
