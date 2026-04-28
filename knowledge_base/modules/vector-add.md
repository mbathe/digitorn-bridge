---
id: vector-add
title: "vector.add (VectorAdd)"
type: module-action
module: vector
action: add
fqn: vector.add
short_name: VectorAdd
keywords: [vector, add, vectoradd, write, ajouter, indexer, embed, insert]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# vector.add (VectorAdd)

## Description
Add text documents to a collection - embeds and indexes them for semantic search.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `collection` | string | ✓ | - | Target collection name. |
| `documents` | array | ✓ | - | List of text documents to embed and store. |
| `ids` | array |  | - | Optional custom IDs for each document. |
| `metadata` | array |  | - | Optional metadata per document. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: vector
      actions: [add]
```

## Aliases
`ajouter`, `indexer`, `embed`, `insert`

## Safety
- Risk level: **medium**
