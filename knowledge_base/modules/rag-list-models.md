---
id: rag-list-models
title: "rag.list_models (RagListModels)"
type: module-action
module: rag
action: list_models
fqn: rag.list_models
short_name: RagListModels
keywords: [rag, list_models, raglistmodels, embeddings]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# rag.list_models (RagListModels)

## Description
List available embedding models (built-in shortcuts and current default).

## Parameters
_(no parameters)_

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: rag
      actions: [list_models]
```

## Safety
- Risk level: **low**
