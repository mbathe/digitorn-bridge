---
id: rag-create-knowledge-base
title: "rag.create_knowledge_base (RagCreateKnowledgeBase)"
type: module-action
module: rag
action: create_knowledge_base
fqn: rag.create_knowledge_base
short_name: RagCreateKnowledgeBase
keywords: [rag, create_knowledge_base, ragcreateknowledgebase, knowledge-base]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# rag.create_knowledge_base (RagCreateKnowledgeBase)

## Description
Create a new knowledge base for storing and searching documents.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `name` | string | ✓ | — | Knowledge base name. |
| `description` | string |  | `` | Human-readable description. |
| `embedding_model` | string |  | `` | Override the default embedding model for this KB. Empty = use module default. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: rag
      actions: [create_knowledge_base]
```

## Safety
- Risk level: **low**
