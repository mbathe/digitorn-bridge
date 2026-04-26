---
id: hook-action-transform_result
title: "Hook action: transform_result"
type: hook-action
action: transform_result
keywords: [transform_result, action, hook, transformation]
---

# Hook action: `transform_result`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action("transform_result")`.

## Params
| Param | Requirement |
|-------|-------------|
| `transformation` | required |

## Behavior
Modify tool result after execution. Only works with post_tool_use.

Params:
    append_to_result (str): Text to append to the tool result.
    inject_note (str): System message to inject after the result.

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": turn_end
    condition:
      type: always
    action:
      type: transform_result
      # params: transformation
```
