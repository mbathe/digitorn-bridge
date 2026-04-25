---
id: widget-set-state
title: "widget.set_state (WidgetSetState)"
type: module-action
module: widget
action: set_state
fqn: widget.set_state
short_name: WidgetSetState
keywords: [widget, set_state, widgetsetstate, ui]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# widget.set_state (WidgetSetState)

## Description
Write into the session's widget state — visible to the agent and other modules.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `set` | object | ✓ | — | Key/value pairs to merge into the session state. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: widget
      actions: [set_state]
```

## Safety
- Risk level: **low**
