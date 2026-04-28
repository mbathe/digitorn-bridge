---
id: workspace-delete
title: "workspace.delete (WsDelete)"
type: module-action
module: workspace
action: delete
fqn: workspace.delete
short_name: WsDelete
keywords: [workspace, delete, wsdelete, files]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# workspace.delete (WsDelete)

## Description
Delete a file from the workspace.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `path` | string | ✓ | - | File path to delete. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: workspace
      actions: [delete]
```

## Safety
- Risk level: **low**
