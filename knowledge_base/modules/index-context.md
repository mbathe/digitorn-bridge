---
id: index-context
title: "index.context (IndexContext)"
type: module-action
module: index
action: context
fqn: index.context
short_name: IndexContext
keywords: [index, context, indexcontext, llm]
permissions: [index:read]
risk_level: low
irreversible: false
require_approval: false
---

# index.context (IndexContext)

## Description
Get optimal context for an LLM to work on a target. Returns the target's signature, location, and related entries (dependencies, callers), all trimmed to fit the token budget. This is the primary action for LLM agents — call this FIRST before reading or editing files.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `target` | string | ✓ | — | What to get context for — can be an entry_id, a file path, or a search query (e.g. 'calculate_discount', '/project/pricing.py'). |
| `token_budget` | integer |  | `4000` | Maximum approximate tokens for the returned context. |
| `include_relations` | boolean |  | `True` | Include related entries (dependencies, callers) in the context. |
| `depth` | integer |  | `1` | Relation traversal depth. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: index
      actions: [context]
```

## Safety
- Required permissions: `index:read`
- Risk level: **low**
