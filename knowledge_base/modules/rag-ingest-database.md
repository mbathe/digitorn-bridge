---
id: rag-ingest-database
title: "rag.ingest_database (RagIngestDatabase)"
type: module-action
module: rag
action: ingest_database
fqn: rag.ingest_database
short_name: RagIngestDatabase
keywords: [rag, ingest_database, ragingestdatabase, ingest, database]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# rag.ingest_database (RagIngestDatabase)

## Description
Ingest database tables into a knowledge base (schema and/or rows).

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `knowledge_base` | string | ✓ | - | Target knowledge base. |
| `connection_id` | string | ✓ | - | Database connection ID. |
| `tables` | object | ✓ | - | Table configs: {table_name: {columns, mode, template, max_rows}}. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: rag
      actions: [ingest_database]
```

## Safety
- Risk level: **medium**
