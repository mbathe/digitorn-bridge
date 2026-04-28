---
id: llm_provider-update-defaults
title: "llm_provider.update_defaults (UpdateDefaults)"
type: module-action
module: llm_provider
action: update_defaults
fqn: llm_provider.update_defaults
short_name: UpdateDefaults
keywords: [llm_provider, update_defaults, updatedefaults, configuration]
permissions: [llm_provider:admin]
risk_level: low
irreversible: false
require_approval: false
---

# llm_provider.update_defaults (UpdateDefaults)

## Description
Update default generation parameters (temperature, max_tokens, top_p) for an existing provider instance. These defaults apply to all subsequent chat requests unless overridden per-request.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `provider_id` | string | ✓ | - | Name of the provider instance to update. |
| `temperature` | number |  | - | Default sampling temperature. |
| `max_tokens` | integer |  | - | Default max tokens. |
| `top_p` | number |  | - | Default nucleus sampling threshold. |
| `extra` | object |  | - | Additional default parameters to merge. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: llm_provider
      actions: [update_defaults]
```

## Safety
- Required permissions: `llm_provider:admin`
- Risk level: **low**
