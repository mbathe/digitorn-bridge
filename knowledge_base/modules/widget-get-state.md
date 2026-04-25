---
id: widget-get-state
title: "widget.get_state (WidgetGetState)"
type: module-action
module: widget
action: get_state
fqn: widget.get_state
short_name: WidgetGetState
keywords: [widget, get_state, widgetgetstate, ui]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# widget.get_state (WidgetGetState)

## Description
Read the session's widget state (or one key via dotted path).

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `key` | string |  | — | Optional dotted-path key to read a single value, e.g. 'form.email' or 'results.rag.query'. When omitted, the full snapshot is returned. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: widget
      actions: [get_state]
```

## Safety
- Risk level: **low**
