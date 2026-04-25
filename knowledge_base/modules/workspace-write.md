---
id: workspace-write
title: "workspace.write (WsWrite)"
type: module-action
module: workspace
action: write
fqn: workspace.write
short_name: WsWrite
keywords: [workspace, write, wswrite, files]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# workspace.write (WsWrite)

## Description
Create or overwrite a file. Streams live to the client.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `path` | string | ✓ | — | File path, e.g. src/App.tsx |
| `content` | string | ✓ | — | Full file content. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: workspace
      actions: [write]
```

## Safety
- Risk level: **low**
