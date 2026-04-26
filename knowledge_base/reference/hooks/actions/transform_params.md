---
id: hook-action-transform_params
title: "Hook action: transform_params"
type: hook-action
action: transform_params
keywords: [transform_params, action, hook, transformation]
---

# Hook action: `transform_params`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action("transform_params")`.

## Params
| Param | Requirement |
|-------|-------------|
| `transformation` | required |

## Behavior
Modify tool parameters before execution. Only works with pre_tool_use.

Params:
    set (dict): Key-value pairs to set/override in tool params.
    remove (list): Keys to remove from tool params.

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": turn_end
    condition:
      type: always
    action:
      type: transform_params
      # params: transformation
```
