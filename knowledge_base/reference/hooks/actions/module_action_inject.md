---
id: hook-action-module_action_inject
title: "Hook action: module_action_inject"
type: hook-action
action: module_action_inject
keywords: [module_action_inject, action, hook, module, params, action_params, role]
---

# Hook action: `module_action_inject`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action("module_action_inject")`.

## Params
| Param | Requirement |
|-------|-------------|
| `action` | required |
| `action_params` | optional |
| `module` | required |
| `params` | optional |
| `role` | optional |

## Behavior
Execute a module action and inject its result into the conversation.

Like ``module_action`` but the result is injected as a system message
so the agent sees it immediately. Designed for real-time feedback loops
like LSP diagnostics after file edits.

Params:
    name (str): Tool name in "module.action" format. Required.
    action_params (dict): Parameters for the action. Default: {}
    format (str): How to format the result. Default: "auto"
        - "auto": only inject if there are errors/warnings
        - "always": always inject the result
    prefix (str): Text prefix for the injected message. Default: ""

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": turn_end
    condition:
      type: always
    action:
      type: module_action_inject
      # params: module, action, params, action_params, role
```
