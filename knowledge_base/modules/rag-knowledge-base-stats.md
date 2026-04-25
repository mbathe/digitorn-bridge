---
id: rag-knowledge-base-stats
title: "rag.knowledge_base_stats (RagKnowledgeBaseStats)"
type: module-action
module: rag
action: knowledge_base_stats
fqn: rag.knowledge_base_stats
short_name: RagKnowledgeBaseStats
keywords: [rag, knowledge_base_stats, ragknowledgebasestats, knowledge-base]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# rag.knowledge_base_stats (RagKnowledgeBaseStats)

## Description
Get detailed statistics for a knowledge base.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `name` | string | ✓ | — | Knowledge base name. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: rag
      actions: [knowledge_base_stats]
```

## Safety
- Risk level: **low**
