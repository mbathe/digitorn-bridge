---
id: hook-action-enforce_compile_fix
title: "Hook action: enforce_compile_fix"
type: hook-action
action: enforce_compile_fix
keywords: [enforce_compile_fix, action, hook, max_reminders]
---

# Hook action: `enforce_compile_fix`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action("enforce_compile_fix")`.

## Params
| Param | Requirement |
|-------|-------------|
| `max_reminders` | optional |

## Behavior
Turn-end guard: if ``_state/compile.json`` shows errors AND no
newer YAML write happened this turn, append a system note so the
next turn is forced to iterate the YAML fix. Prevents the agent from
giving up on a half-broken app.yaml (the `compile_on_app_yaml_write`
hook already injects errors into the write-tool result; this hook
catches the case where the agent sees them and then stops responding
without correcting).

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": turn_end
    condition:
      type: always
    action:
      type: enforce_compile_fix
      # params: max_reminders
```
