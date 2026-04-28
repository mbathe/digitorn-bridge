---
id: llm_provider-get-provider-info
title: "llm_provider.get_provider_info (GetProviderInfo)"
type: module-action
module: llm_provider
action: get_provider_info
fqn: llm_provider.get_provider_info
short_name: GetProviderInfo
keywords: [llm_provider, get_provider_info, getproviderinfo, query, info]
permissions: [llm_provider:read]
risk_level: low
irreversible: false
require_approval: false
---

# llm_provider.get_provider_info (GetProviderInfo)

## Description
Get detailed metadata about a configured provider instance including capabilities.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `provider_id` | string | ✓ | - | Name of the provider instance. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: llm_provider
      actions: [get_provider_info]
```

## Safety
- Required permissions: `llm_provider:read`
- Risk level: **low**
