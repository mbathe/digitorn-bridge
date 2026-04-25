---
id: vector-create-collection
title: "vector.create_collection (VectorCreateCollection)"
type: module-action
module: vector
action: create_collection
fqn: vector.create_collection
short_name: VectorCreateCollection
keywords: [vector, create_collection, vectorcreatecollection, admin, creer_collection, new_collection]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# vector.create_collection (VectorCreateCollection)

## Description
Create a new vector collection for storing and searching embedded documents.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `name` | string | ✓ | — | Collection name (alphanumeric + hyphens). |
| `description` | string |  | `` | Human-readable description of what this collection stores. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: vector
      actions: [create_collection]
```

## Aliases
`creer_collection`, `new_collection`

## Safety
- Risk level: **medium**
