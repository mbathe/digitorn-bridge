---
id: vector-list-collections
title: "vector.list_collections (VectorListCollections)"
type: module-action
module: vector
action: list_collections
fqn: vector.list_collections
short_name: VectorListCollections
keywords: [vector, list_collections, vectorlistcollections, info, lister_collections]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# vector.list_collections (VectorListCollections)

## Description
List all vector collections with their document counts.

## Parameters
_(no parameters)_

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: vector
      actions: [list_collections]
```

## Aliases
`lister_collections`

## Safety
- Risk level: **low**
