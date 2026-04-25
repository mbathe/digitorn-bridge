---
id: rag-query
title: "rag.query (RagQuery)"
type: module-action
module: rag
action: query
fqn: rag.query
short_name: RagQuery
keywords: [rag, query, ragquery, search]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# rag.query (RagQuery)

## Description
Search a knowledge base using the configured retrieval pipeline with citations.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `knowledge_base` | string | ✓ | — | Knowledge base to search. |
| `query` | string | ✓ | — | Search query. |
| `top_k` | integer |  | `5` | Number of results to return. |
| `min_score` | number |  | `0.0` | Minimum relevance score filter. |
| `strategy` | string |  | `` | Override retrieval strategy. Empty = use pipeline default. |
| `filter` | object |  | — | Metadata filter (backend-specific). |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: rag
      actions: [query]
```

## Safety
- Risk level: **low**
