---
id: hook-action-inject_message
title: "Hook action: inject_message"
type: hook-action
action: inject_message
keywords: [inject_message, action, hook, content, role, placeholder]
---

# Hook action: `inject_message`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action("inject_message")`.

## Params
| Param | Requirement |
|-------|-------------|
| `content` | required |
| `placeholder` | optional |
| `role` | optional |

## Behavior
Inject content into the conversation that the LLM WILL see.

Three strategies (auto-selected for provider compatibility):
- "append_to_system": Appends to the system prompt (safest, all providers)
- "append_to_last_user": Appends to the last user message (visible in conversation)
- "new_message": Creates a new message (may break user/assistant alternation)

Params:
    content (str): Content to inject. Required.
    strategy (str): "auto" (default), "system", "user", or "new_message".
        - "auto": appends to last user message (most compatible)
        - "system": appends to system prompt
        - "user": appends to last user message
        - "new_message": creates a separate message (may not work with all providers)
    role (str): Only used with strategy="new_message". Default: "user".
    position (str): Only used with strategy="new_message". "before_last" or "end".

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": turn_end
    condition:
      type: always
    action:
      type: inject_message
      # params: content, role, placeholder
```
