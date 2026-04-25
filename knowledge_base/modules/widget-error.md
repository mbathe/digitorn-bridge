---
id: widget-error
title: "widget.error (WidgetError)"
type: module-action
module: widget
action: error
fqn: widget.error
short_name: WidgetError
keywords: [widget, error, widgeterror, ui]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# widget.error (WidgetError)

## Description
Surface an error in a widget (e.g. failed data binding) without closing it.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `widget_id` | string | ✓ | — |  |
| `binding` | string |  | — | Optional name of the data binding that failed. |
| `message` | string | ✓ | — | Human-readable error. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: widget
      actions: [error]
```

## Safety
- Risk level: **low**
