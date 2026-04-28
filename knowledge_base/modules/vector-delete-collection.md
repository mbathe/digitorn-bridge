---
id: vector-delete-collection
title: "vector.delete_collection (VectorDeleteCollection)"
type: module-action
module: vector
action: delete_collection
fqn: vector.delete_collection
short_name: VectorDeleteCollection
keywords: [vector, delete_collection, vectordeletecollection, admin, supprimer_collection]
permissions: []
risk_level: high
irreversible: true
require_approval: false
---

# vector.delete_collection (VectorDeleteCollection)

## Description
Delete a vector collection and all its documents permanently.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `name` | string | ✓ | - | Collection name to delete. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: vector
      actions: [delete_collection]
```

## Aliases
`supprimer_collection`

## Safety
- Risk level: **high**
- ⚠️ **Irreversible** - cannot be undone once executed
