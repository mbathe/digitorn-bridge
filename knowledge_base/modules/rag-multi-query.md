---
id: rag-multi-query
title: "rag.multi_query (RagMultiQuery)"
type: module-action
module: rag
action: multi_query
fqn: rag.multi_query
short_name: RagMultiQuery
keywords: [rag, multi_query, ragmultiquery, search, advanced]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# rag.multi_query (RagMultiQuery)

## Description
Search with LLM-generated query variants for broader recall (MultiQuery RAG).

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `knowledge_base` | string | ✓ | — | Knowledge base to search. |
| `query` | string | ✓ | — | Search query. |
| `top_k` | integer |  | `5` | Number of results to return. |
| `num_variants` | integer |  | `3` | Number of query variants to generate. |
| `min_score` | number |  | `0.0` | Minimum relevance score filter. |
| `filter` | object |  | — | Metadata filter. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: rag
      actions: [multi_query]
```

## Safety
- Risk level: **low**
