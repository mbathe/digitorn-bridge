---
id: hook-action-compact_context
title: "Hook action: compact_context"
type: hook-action
action: compact_context
keywords: [compact_context, action, hook, strategy, keep_last]
---

# Hook action: `compact_context`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action("compact_context")`.

## Params
| Param | Requirement |
|-------|-------------|
| `keep_last` | optional |
| `strategy` | optional |

## Behavior
Compact the message history to reduce token usage.

After compaction, re-injects a context reminder so the model
retains awareness of its tools and capabilities.

Strategies:
    - truncate: Keep system + last N messages (fast, no LLM call)
    - summarize: Use LLM to summarize older messages (smart, 1 LLM call)

Params:
    strategy (str): "truncate" or "summarize". Default: "summarize"
    keep_recent (int): Number of recent messages to preserve. Default: 10
    summary_max_tokens (int): Max tokens for the summary. Default: 1024
    summary_prompt (str): Custom prompt for summarization.
    target_pressure (float): Compact until below this pressure. Default: 0.5

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": turn_end
    condition:
      type: always
    action:
      type: compact_context
      # params: strategy, keep_last
```
