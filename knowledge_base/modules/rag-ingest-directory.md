---
id: rag-ingest-directory
title: "rag.ingest_directory (RagIngestDirectory)"
type: module-action
module: rag
action: ingest_directory
fqn: rag.ingest_directory
short_name: RagIngestDirectory
keywords: [rag, ingest_directory, ragingestdirectory, ingest]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# rag.ingest_directory (RagIngestDirectory)

## Description
Ingest all matching files from a directory into a knowledge base.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `knowledge_base` | string | ✓ | - | Target knowledge base name. |
| `path` | string | ✓ | - | Directory path. |
| `extensions` | array |  | - | File extensions to include. |
| `recursive` | boolean |  | `True` | Recurse into subdirectories. |
| `max_files` | integer |  | `1000` | Maximum files to process. |
| `chunk_strategy` | string |  | `` | Chunking strategy override. |
| `chunk_size` | integer |  | `0` | Chunk size override. |
| `chunk_overlap` | integer |  | `-1` | Chunk overlap override. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: rag
      actions: [ingest_directory]
```

## Safety
- Risk level: **low**
