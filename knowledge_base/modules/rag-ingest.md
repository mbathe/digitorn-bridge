---
id: rag-ingest
title: "rag.ingest (RagIngest)"
type: module-action
module: rag
action: ingest
fqn: rag.ingest
short_name: RagIngest
keywords: [rag, ingest, ragingest]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# rag.ingest (RagIngest)

## Description
Ingest raw text documents into a knowledge base.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `knowledge_base` | string | ✓ | — | Target knowledge base name. |
| `documents` | array | ✓ | — | Text documents to ingest. |
| `ids` | array |  | — | Optional document IDs (auto-generated if omitted). |
| `metadata` | array |  | — | Optional per-document metadata. |
| `source_type` | string |  | `manual` | Source type for citations (manual, file, database, web). |
| `source_id` | string |  | `` | Source identifier for citations. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: rag
      actions: [ingest]
```

## Safety
- Risk level: **low**
