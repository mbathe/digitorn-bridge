---
id: vector-add-file
title: "vector.add_file (VectorAddFile)"
type: module-action
module: vector
action: add_file
fqn: vector.add_file
short_name: VectorAddFile
keywords: [vector, add_file, vectoraddfile, write, indexer_fichier, embed_file]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# vector.add_file (VectorAddFile)

## Description
Read a file, split it into chunks, embed each chunk, and add to a collection.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `collection` | string | ✓ | — | Target collection name. |
| `path` | string | ✓ | — | File path to read and index. |
| `chunk_strategy` | string |  | `recursive` | Chunking strategy: 'fixed', 'sentence', 'paragraph', or 'recursive'. |
| `chunk_size` | integer |  | `500` | Target chunk size in characters. |
| `overlap` | integer |  | `50` | Overlap between chunks in characters. |
| `metadata` | object |  | — | Extra metadata to attach to all chunks. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: vector
      actions: [add_file]
```

## Aliases
`indexer_fichier`, `embed_file`

## Safety
- Risk level: **medium**
