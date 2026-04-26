---
id: hook-action-enforce_phase6
title: "Hook action: enforce_phase6"
type: hook-action
action: enforce_phase6
keywords: [enforce_phase6, action, hook, max_reminders]
---

# Hook action: `enforce_phase6`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action("enforce_phase6")`.

## Params
| Param | Requirement |
|-------|-------------|
| `max_reminders` | optional |

## Behavior
Turn-end guard: if a deploy happened but no successful smoke test
exists, append a system note to the last user message so the next
turn re-fires Phase 6. Silently no-ops if the workspace module is
absent or both files are missing.

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": turn_end
    condition:
      type: always
    action:
      type: enforce_phase6
      # params: max_reminders
```
