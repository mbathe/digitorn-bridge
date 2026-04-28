---
id: index-scan
title: "index.scan (IndexScan)"
type: module-action
module: index
action: scan
fqn: index.scan
short_name: IndexScan
keywords: [index, scan, indexscan]
permissions: [index:write]
risk_level: low
irreversible: false
require_approval: false
---

# index.scan (IndexScan)

## Description
Scan a registered source and update the index. Incremental by default - only processes changed content. Use force=true for a full rescan.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `source_id` | string | ✓ | - | Source to scan (must be registered first). |
| `force` | boolean |  | `False` | Force full rescan even if content hashes haven't changed. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: index
      actions: [scan]
```

## Safety
- Required permissions: `index:write`
- Risk level: **low**
