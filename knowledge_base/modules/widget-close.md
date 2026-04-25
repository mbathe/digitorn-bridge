---
id: widget-close
title: "widget.close (WidgetClose)"
type: module-action
module: widget
action: close
fqn: widget.close
short_name: WidgetClose
keywords: [widget, close, widgetclose, ui]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# widget.close (WidgetClose)

## Description
Close (unmount) a previously rendered widget.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `widget_id` | string | ✓ | — | Widget to unmount. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: widget
      actions: [close]
```

## Safety
- Risk level: **low**
