---
id: widget-clear
title: "widget.clear (WidgetClear)"
type: module-action
module: widget
action: clear
fqn: widget.clear
short_name: WidgetClear
keywords: [widget, clear, widgetclear, ui]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# widget.clear (WidgetClear)

## Description
Clear all mounted widgets and state for the session.

## Parameters
_(no parameters)_

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: widget
      actions: [clear]
```

## Safety
- Risk level: **low**
