---
id: hook-condition-content_contains
title: "Hook condition: content_contains"
type: hook-condition
condition: content_contains
keywords: [content_contains, condition, hook, keyword]
---

# Hook condition: `content_contains`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_condition("content_contains")`.

## Params
| Param | Requirement |
|-------|-------------|
| `keyword` | required |

## Behavior
Fire when message content contains a keyword.

Params:
    keyword (str): Text to search for (case-insensitive)

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": tool_start     # any hook event
    condition:
      type: content_contains
      # params: keyword
    action:
      type: log
      message: "content_contains fired"
```
