---
id: rag-delete-knowledge-base
title: "rag.delete_knowledge_base (RagDeleteKnowledgeBase)"
type: module-action
module: rag
action: delete_knowledge_base
fqn: rag.delete_knowledge_base
short_name: RagDeleteKnowledgeBase
keywords: [rag, delete_knowledge_base, ragdeleteknowledgebase, knowledge-base]
permissions: []
risk_level: high
irreversible: true
require_approval: false
---

# rag.delete_knowledge_base (RagDeleteKnowledgeBase)

## Description
Delete a knowledge base and all its data.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `name` | string | ✓ | — | Knowledge base name to delete. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: rag
      actions: [delete_knowledge_base]
```

## Safety
- Risk level: **high**
- ⚠️ **Irreversible** — cannot be undone once executed
