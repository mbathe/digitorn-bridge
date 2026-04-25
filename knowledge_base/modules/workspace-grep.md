---
id: workspace-grep
title: "workspace.grep (WsGrep)"
type: module-action
module: workspace
action: grep
fqn: workspace.grep
short_name: WsGrep
keywords: [workspace, grep, wsgrep, files]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# workspace.grep (WsGrep)

## Description
Search file contents by regex pattern.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `pattern` | string | ✓ | — | Regex pattern to search for. |
| `glob` | string |  | — | Glob filter, e.g. *.tsx |
| `case_insensitive` | boolean |  | `False` | Case-insensitive. |
| `multiline` | boolean |  | `False` | Multiline mode. |
| `before` | integer |  | `0` | Context lines before. |
| `after` | integer |  | `0` | Context lines after. |
| `max_results` | integer |  | `200` | Max results. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: workspace
      actions: [grep]
```

## Safety
- Risk level: **low**
