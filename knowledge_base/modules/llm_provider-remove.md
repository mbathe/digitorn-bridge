---
id: llm_provider-remove
title: "llm_provider.remove (Remove)"
type: module-action
module: llm_provider
action: remove
fqn: llm_provider.remove
short_name: Remove
keywords: [llm_provider, remove, configuration, lifecycle]
permissions: [llm_provider:admin]
risk_level: low
irreversible: false
require_approval: false
---

# llm_provider.remove (Remove)

## Description
Remove a configured LLM provider instance and release its resources.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `provider_id` | string | ✓ | - | Name of the provider instance to remove. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: llm_provider
      actions: [remove]
```

## Safety
- Required permissions: `llm_provider:admin`
- Risk level: **low**
