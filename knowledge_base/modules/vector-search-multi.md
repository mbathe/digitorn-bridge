---
id: vector-search-multi
title: "vector.search_multi (VectorSearchMulti)"
type: module-action
module: vector
action: search_multi
fqn: vector.search_multi
short_name: VectorSearchMulti
keywords: [vector, search_multi, vectorsearchmulti, search, recherche_multiple, multi_search, cross_search]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# vector.search_multi (VectorSearchMulti)

## Description
Search across multiple collections and merge results by score.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `collections` | array | ✓ | — | Collection names to search. |
| `query` | string | ✓ | — | Natural language search query. |
| `top_k` | integer |  | `5` | Total results to return (merged across collections). |
| `min_score` | number |  | `0.3` | Minimum similarity score threshold. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: vector
      actions: [search_multi]
```

## Aliases
`recherche_multiple`, `multi_search`, `cross_search`

## Safety
- Risk level: **low**
