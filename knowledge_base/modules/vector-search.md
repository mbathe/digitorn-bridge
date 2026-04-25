---
id: vector-search
title: "vector.search (VectorSearch)"
type: module-action
module: vector
action: search
fqn: vector.search
short_name: VectorSearch
keywords: [vector, search, vectorsearch, rechercher, chercher, query, find_similar]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# vector.search (VectorSearch)

## Description
Semantic search — find documents similar to a natural language query.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `collection` | string | ✓ | — | Collection to search. |
| `query` | string | ✓ | — | Natural language search query. |
| `top_k` | integer |  | `5` | Number of results to return. |
| `min_score` | number |  | `0.3` | Minimum similarity score threshold. |
| `filter` | object |  | — | Metadata filter (Qdrant payload filter format). |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: vector
      actions: [search]
```

## Aliases
`rechercher`, `chercher`, `query`, `find_similar`

## Safety
- Risk level: **low**
