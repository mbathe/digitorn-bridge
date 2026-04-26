---
id: hook-action-log
title: "Hook action: log"
type: hook-action
action: log
keywords: [log, action, hook, message, level]
---

# Hook action: `log`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action("log")`.

## Params
| Param | Requirement |
|-------|-------------|
| `level` | optional |
| `message` | required |

## Behavior
Log a message. Useful for debugging hooks.

Params:
    message (str): Log message template. Supports {turn}, {tokens}, {tools}, {messages}.
    level (str): Log level. Default: "info"

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": turn_end
    condition:
      type: always
    action:
      type: log
      # params: message, level
```
