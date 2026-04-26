---
id: hook-condition-always
title: "Hook condition: always"
type: hook-condition
condition: always
keywords: [always, condition, hook]
---

# Hook condition: `always`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_condition("always")`.

## Params
_(no params)_

## Behavior
Always fire. Useful with cooldown for periodic actions.

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": tool_start     # any hook event
    condition:
      type: always
    action:
      type: log
      message: "always fired"
```
