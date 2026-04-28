---
id: workspace-read
title: "workspace.read (WsRead)"
type: module-action
module: workspace
action: read
fqn: workspace.read
short_name: WsRead
keywords: [workspace, read, wsread, files]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# workspace.read (WsRead)

## Description
Read a file from the workspace.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `path` | string | ✓ | - | File path to read. |
| `offset` | integer |  | - | 1-indexed start line. |
| `limit` | integer |  | - | Max lines to return. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: workspace
      actions: [read]
```

## Safety
- Risk level: **low**
