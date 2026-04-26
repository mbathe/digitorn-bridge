---
id: hook-action-prefetch_ground_truth
title: "Hook action: prefetch_ground_truth"
type: hook-action
action: prefetch_ground_truth
keywords: [prefetch_ground_truth, action, hook, include_modules, include_triggers, include_templates, include_examples, examples_query, examples_k]
---

# Hook action: `prefetch_ground_truth`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action("prefetch_ground_truth")`.

## Params
| Param | Requirement |
|-------|-------------|
| `examples_k` | optional |
| `examples_query` | optional |
| `include_examples` | optional |
| `include_modules` | optional |
| `include_templates` | optional |
| `include_triggers` | optional |

## Behavior
Turn-0 single-shot: preload modules/triggers/templates + a few
canonical RAG examples into the conversation so the agent cannot
skip Phase 0 discovery. Runs once per session.

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": turn_end
    condition:
      type: always
    action:
      type: prefetch_ground_truth
      # params: include_modules, include_triggers, include_templates, include_examples, examples_query, examples_k
```
