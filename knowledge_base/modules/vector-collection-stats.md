---
id: vector-collection-stats
title: "vector.collection_stats (VectorCollectionStats)"
type: module-action
module: vector
action: collection_stats
fqn: vector.collection_stats
short_name: VectorCollectionStats
keywords: [vector, collection_stats, vectorcollectionstats, info, statistiques_collection, info_collection]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# vector.collection_stats (VectorCollectionStats)

## Description
Get detailed statistics for a collection: document count, vector dimensions, storage info.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `collection` | string | ✓ | - | Collection name. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: vector
      actions: [collection_stats]
```

## Aliases
`statistiques_collection`, `info_collection`

## Safety
- Risk level: **low**
