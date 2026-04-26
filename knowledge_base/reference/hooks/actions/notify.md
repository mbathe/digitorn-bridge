---
id: hook-action-notify
title: "Hook action: notify"
type: hook-action
action: notify
keywords: [notify, action, hook, title, message, level, tag]
---

# Hook action: `notify`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action("notify")`.

## Params
| Param | Requirement |
|-------|-------------|
| `level` | optional |
| `message` | optional |
| `tag` | optional |
| `title` | optional |

## Behavior
Send a notification to the client via SSE.

Params:
    title (str): Notification title.
    message (str): Notification body.
    level (str): "info", "warning", "error". Default: "info"

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": turn_end
    condition:
      type: always
    action:
      type: notify
      # params: title, message, level, tag
```
