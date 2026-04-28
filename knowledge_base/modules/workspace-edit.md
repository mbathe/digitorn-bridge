---
id: workspace-edit
title: "workspace.edit (WsEdit)"
type: module-action
module: workspace
action: edit
fqn: workspace.edit
short_name: WsEdit
keywords: [workspace, edit, wsedit, files]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# workspace.edit (WsEdit)

## Description
Surgical text replacement in an existing file.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `path` | string | ✓ | - | File path to edit. |
| `old_string` | string |  | - | Exact text to find (must be unique). |
| `new_string` | string |  | `` | Replacement text. |
| `replace_all` | boolean |  | `False` | Replace all occurrences. |
| `insert_at_line` | integer |  | - | Insert before this line (1-indexed). Omit old_string when using this. |
| `fuzzy_threshold` | number |  | `0.85` | Fuzzy match threshold. |
| `max_suggestions` | integer |  | `3` | Max suggestions on failure. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: workspace
      actions: [edit]
```

## Safety
- Risk level: **low**
