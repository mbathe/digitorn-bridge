---
id: vector-count
title: "vector.count (VectorCount)"
type: module-action
module: vector
action: count
fqn: vector.count
short_name: VectorCount
keywords: [vector, count, vectorcount, info, compter, count_docs]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# vector.count (VectorCount)

## Description
Count documents in a collection.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `collection` | string | ✓ | - | Collection name. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: vector
      actions: [count]
```

## Aliases
`compter`, `count_docs`

## Safety
- Risk level: **low**
