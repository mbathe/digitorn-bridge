---
id: hook-condition-never
title: "Hook condition: never"
type: hook-condition
condition: never
keywords: [never, condition, hook]
---

# Hook condition: `never`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_condition("never")`.

## Params
_(no params)_

## Behavior
Never fire. Useful as a temporary kill-switch from YAML without
removing a hook definition.

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": tool_start     # any hook event
    condition:
      type: never
    action:
      type: log
      message: "never fired"
```
