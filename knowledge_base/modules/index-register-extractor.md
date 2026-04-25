---
id: index-register-extractor
title: "index.register_extractor (IndexRegisterExtractor)"
type: module-action
module: index
action: register_extractor
fqn: index.register_extractor
short_name: IndexRegisterExtractor
keywords: [index, register_extractor, indexregisterextractor, config]
permissions: [index:admin]
risk_level: low
irreversible: false
require_approval: false
---

# index.register_extractor (IndexRegisterExtractor)

## Description
Register a custom extractor provided by another module. The extractor will be called via the service bus during scan.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `name` | string | ✓ | — | Unique name for this extractor (e.g. 'sql', 'pdf', 'markdown'). |
| `module_id` | string | ✓ | — | Module that provides the extraction logic. |
| `extract_action` | string | ✓ | — | Action name on the module that performs extraction. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: index
      actions: [register_extractor]
```

## Safety
- Required permissions: `index:admin`
- Risk level: **low**
