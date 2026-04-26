---
id: hook-condition-context_pressure
title: "Hook condition: context_pressure"
type: hook-condition
condition: context_pressure
keywords: [context_pressure, condition, hook, threshold]
---

# Hook condition: `context_pressure`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_condition("context_pressure")`.

## Params
| Param | Requirement |
|-------|-------------|
| `threshold` | optional |

## Behavior
Fire when estimated token usage exceeds a threshold.

Uses the real model limits from TurnState (set via AgentContext).
Params can override for testing.

Params:
    threshold (float): Pressure ratio (0.0-1.0). Default: 0.75

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": tool_start     # any hook event
    condition:
      type: context_pressure
      # params: threshold
    action:
      type: log
      message: "context_pressure fired"
```
