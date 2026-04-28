---
id: hook-action-module_action
title: "Hook action: module_action"
type: hook-action
action: module_action
keywords: [module_action, action, hook, module, params, action_params]
---

# Hook action: `module_action`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action("module_action")`.

## Params
| Param | Requirement |
|-------|-------------|
| `action` | required |
| `action_params` | optional |
| `module` | required |
| `params` | optional |

## Behavior
Execute a module action via context_builder.

Params (accept any of the following shapes - the schema declares
``module`` + ``action`` but older YAML used a single ``name``):
    module (str) + action (str): split form - preferred.
    name (str): "module.action" legacy shorthand.
    action_params (dict) OR params (dict): tool params.

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": turn_end
    condition:
      type: always
    action:
      type: module_action
      # params: module, action, params, action_params
```
