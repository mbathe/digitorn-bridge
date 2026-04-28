---
id: vector-get
title: "vector.get (VectorGet)"
type: module-action
module: vector
action: get
fqn: vector.get
short_name: VectorGet
keywords: [vector, get, vectorget, read, obtenir_documents, retrieve]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# vector.get (VectorGet)

## Description
Retrieve specific documents by their IDs.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `collection` | string | ✓ | - | Collection name. |
| `ids` | array | ✓ | - | Document IDs to retrieve. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: vector
      actions: [get]
```

## Aliases
`obtenir_documents`, `retrieve`

## Safety
- Risk level: **low**
