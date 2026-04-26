---
id: hook-action-gate
title: "Hook action: gate"
type: hook-action
action: gate
keywords: [gate, action, hook, reason, allow]
---

# Hook action: `gate`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action("gate")`.

## Params
| Param | Requirement |
|-------|-------------|
| `allow` | optional |
| `reason` | optional |

## Behavior
Block tool execution. Only works with pre_tool_use event.

When this fires, the tool is NOT executed and the agent receives
an error message explaining why.

Params:
    reason (str): Why the tool was blocked.

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": turn_end
    condition:
      type: always
    action:
      type: gate
      # params: reason, allow
```
