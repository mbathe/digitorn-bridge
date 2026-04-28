---
id: widget-render
title: "widget.render (WidgetRender)"
type: module-action
module: widget
action: render
fqn: widget.render
short_name: WidgetRender
keywords: [widget, render, widgetrender, ui]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# widget.render (WidgetRender)

## Description
Render a widget in one of the 4 zones (inline, chat_side, workspace, modal).

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `zone` | string | ✓ | - | inline \| chat_side \| workspace \| modal |
| `target` | string |  | - | Tab id (workspace) or modal name (modal). None for inline / chat_side. |
| `widget_id` | string |  | - | Stable id for later update/close. Auto-generated if not provided. |
| `ref` | string |  | - | Name of a pre-declared inline widget in the app's widgets.inline map. |
| `tree` | object |  | - | Inline widget tree. Mutually exclusive with ref. |
| `ctx` | object |  | - | Context bag exposed as ctx.* in the widget tree. |
| `turn_id` | string |  | - | Optional turn id to associate the widget with a chat turn. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: widget
      actions: [render]
```

## Safety
- Risk level: **low**
