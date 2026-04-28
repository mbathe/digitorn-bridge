---
id: rag-ingest-file
title: "rag.ingest_file (RagIngestFile)"
type: module-action
module: rag
action: ingest_file
fqn: rag.ingest_file
short_name: RagIngestFile
keywords: [rag, ingest_file, ragingestfile, ingest]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# rag.ingest_file (RagIngestFile)

## Description
Ingest a file into a knowledge base with automatic chunking.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `knowledge_base` | string | ✓ | - | Target knowledge base name. |
| `path` | string | ✓ | - | File path to ingest. |
| `chunk_strategy` | string |  | `` | Chunking strategy. Empty = use module default. |
| `chunk_size` | integer |  | `0` | Chunk size. 0 = use module default. |
| `chunk_overlap` | integer |  | `-1` | Chunk overlap. -1 = use module default. |
| `metadata` | object |  | - | Extra metadata attached to all chunks. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: rag
      actions: [ingest_file]
```

## Safety
- Risk level: **low**
