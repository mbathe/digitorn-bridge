---
id: workspace-glob
title: "workspace.glob (WsGlob)"
type: module-action
module: workspace
action: glob
fqn: workspace.glob
short_name: WsGlob
keywords: [workspace, glob, wsglob, files]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# workspace.glob (WsGlob)

## Description
Find files by name pattern (e.g. **/*.tsx, slides/*.md).

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `pattern` | string | ✓ | - | Glob pattern, e.g. **/*.tsx, slides/*.md |
| `sort_by` | string |  | `path` | Sort: path, size, lines. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: workspace
      actions: [glob]
```

## Safety
- Risk level: **low**
