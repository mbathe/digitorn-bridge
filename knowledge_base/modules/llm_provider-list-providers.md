---
id: llm_provider-list-providers
title: "llm_provider.list_providers (ListProviders)"
type: module-action
module: llm_provider
action: list_providers
fqn: llm_provider.list_providers
short_name: ListProviders
keywords: [llm_provider, list_providers, listproviders, query, info]
permissions: [llm_provider:read]
risk_level: low
irreversible: false
require_approval: false
---

# llm_provider.list_providers (ListProviders)

## Description
List all configured LLM provider instances with their models and backends.

## Parameters
_(no parameters)_

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: llm_provider
      actions: [list_providers]
```

## Safety
- Required permissions: `llm_provider:read`
- Risk level: **low**
