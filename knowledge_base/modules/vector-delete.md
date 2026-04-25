---
id: vector-delete
title: "vector.delete (VectorDelete)"
type: module-action
module: vector
action: delete
fqn: vector.delete
short_name: VectorDelete
keywords: [vector, delete, vectordelete, write, supprimer_documents, remove]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# vector.delete (VectorDelete)

## Description
Delete documents from a collection by IDs.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `collection` | string | ✓ | — | Collection name. |
| `ids` | array |  | — | Document IDs to delete. |
| `filter` | object |  | — | Metadata filter for bulk deletion. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: vector
      actions: [delete]
```

## Aliases
`supprimer_documents`, `remove`

## Safety
- Risk level: **medium**
