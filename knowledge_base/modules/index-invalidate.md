---
id: index-invalidate
title: "index.invalidate (IndexInvalidate)"
type: module-action
module: index
action: invalidate
fqn: index.invalidate
short_name: IndexInvalidate
keywords: [index, invalidate, indexinvalidate]
permissions: [index:write]
risk_level: low
irreversible: false
require_approval: false
---

# index.invalidate (IndexInvalidate)

## Description
Invalidate (remove) entries from the index. Use source_id to clear an entire source, or path for a specific file.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `source_id` | string |  | - | Invalidate all entries from this source. |
| `path` | string |  | - | Invalidate all entries for this specific path. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: index
      actions: [invalidate]
```

## Safety
- Required permissions: `index:write`
- Risk level: **low**
