---
id: rag-migrate-embeddings
title: "rag.migrate_embeddings (RagMigrateEmbeddings)"
type: module-action
module: rag
action: migrate_embeddings
fqn: rag.migrate_embeddings
short_name: RagMigrateEmbeddings
keywords: [rag, migrate_embeddings, ragmigrateembeddings, embeddings]
permissions: []
risk_level: high
irreversible: false
require_approval: false
---

# rag.migrate_embeddings (RagMigrateEmbeddings)

## Description
Re-embed a knowledge base with a different embedding model.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `knowledge_base` | string | ✓ | - | Knowledge base to migrate. |
| `target_model` | string | ✓ | - | New embedding model (shortcut or ID). |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: rag
      actions: [migrate_embeddings]
```

## Safety
- Risk level: **high**
