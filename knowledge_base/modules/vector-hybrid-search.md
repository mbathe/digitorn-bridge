---
id: vector-hybrid-search
title: "vector.hybrid_search (VectorHybridSearch)"
type: module-action
module: vector
action: hybrid_search
fqn: vector.hybrid_search
short_name: VectorHybridSearch
keywords: [vector, hybrid_search, vectorhybridsearch, search, recherche_hybride]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# vector.hybrid_search (VectorHybridSearch)

## Description
Hybrid search combining semantic similarity with keyword matching for better recall.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `collection` | string | ✓ | - | Collection to search. |
| `query` | string | ✓ | - | Search query. |
| `top_k` | integer |  | `5` | Number of results. |
| `keyword_weight` | number |  | `0.3` | Weight for keyword matching (0-1). |
| `semantic_weight` | number |  | `0.7` | Weight for semantic similarity (0-1). |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: vector
      actions: [hybrid_search]
```

## Aliases
`recherche_hybride`

## Safety
- Risk level: **low**
