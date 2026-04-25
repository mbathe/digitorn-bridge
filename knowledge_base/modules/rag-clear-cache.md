---
id: rag-clear-cache
title: "rag.clear_cache (RagClearCache)"
type: module-action
module: rag
action: clear_cache
fqn: rag.clear_cache
short_name: RagClearCache
keywords: [rag, clear_cache, ragclearcache, cache]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# rag.clear_cache (RagClearCache)

## Description
Clear the semantic cache for faster-but-stale response prevention.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `knowledge_base` | string |  | `` | Clear cache for a specific KB. Empty = clear all. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: rag
      actions: [clear_cache]
```

## Safety
- Risk level: **low**
