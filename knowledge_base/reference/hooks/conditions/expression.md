---
id: hook-condition-expression
title: "Hook condition: expression"
type: hook-condition
condition: expression
keywords: [expression, condition, hook, expr]
---

# Hook condition: `expression`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_condition("expression")`.

## Params
| Param | Requirement |
|-------|-------------|
| `expr` | required |

## Behavior
Fire when a Python expression evaluates to True.

Params:
    expr (str): Python expression. Available vars: turn, tools, messages, pressure, tokens

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": tool_start     # any hook event
    condition:
      type: expression
      # params: expr
    action:
      type: log
      message: "expression fired"
```
