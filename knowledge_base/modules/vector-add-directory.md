---
id: vector-add-directory
title: "vector.add_directory (VectorAddDirectory)"
type: module-action
module: vector
action: add_directory
fqn: vector.add_directory
short_name: VectorAddDirectory
keywords: [vector, add_directory, vectoradddirectory, write, batch, indexer_repertoire, embed_directory, index_folder]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# vector.add_directory (VectorAddDirectory)

## Description
Index all files in a directory — walks the tree, chunks each file, embeds, and stores. Skips unchanged files (dedup).

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `collection` | string | ✓ | — | Target collection name. |
| `path` | string | ✓ | — | Directory path to index. |
| `extensions` | array |  | `['.txt', '.md', '.py', '.json', '.yaml', '.yml', '.rst', '.csv', '.log']` | File extensions to include. |
| `chunk_strategy` | string |  | `recursive` | Chunking strategy: 'fixed', 'sentence', 'paragraph', or 'recursive'. |
| `chunk_size` | integer |  | `500` | Target chunk size in characters. |
| `overlap` | integer |  | `50` | Overlap between chunks in characters. |
| `recursive` | boolean |  | `True` | Recurse into subdirectories. |
| `max_files` | integer |  | `100` | Maximum files to index. |
| `metadata` | object |  | — | Extra metadata to attach to all chunks. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: vector
      actions: [add_directory]
```

## Aliases
`indexer_repertoire`, `embed_directory`, `index_folder`

## Safety
- Risk level: **medium**
