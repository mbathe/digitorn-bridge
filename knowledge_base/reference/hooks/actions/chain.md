---
id: hook-action-chain
title: "Hook action: chain"
type: hook-action
action: chain
keywords: [chain, action, hook, actions]
---

# Hook action: `chain`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action("chain")`.

## Params
| Param | Requirement |
|-------|-------------|
| `actions` | required |

## Behavior
Execute multiple actions in sequence.

Params:
    actions (list): List of {type, params} action definitions.

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": turn_end
    condition:
      type: always
    action:
      type: chain
      # params: actions
```
