---
id: widget-update
title: "widget.update (WidgetUpdate)"
type: module-action
module: widget
action: update
fqn: widget.update
short_name: WidgetUpdate
keywords: [widget, update, widgetupdate, ui]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# widget.update (WidgetUpdate)

## Description
Patch a previously rendered widget (data.X, state.X, ctx.X).

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `widget_id` | string | ✓ | — | The id returned by the render call. |
| `patch` | object | ✓ | — | Dotted-path keys: 'data.sources', 'state.filter', 'ctx.path'… |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: widget
      actions: [update]
```

## Safety
- Risk level: **low**
