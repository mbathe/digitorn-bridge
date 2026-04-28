---
id: yaml-schema-workspacetabwidget
title: "WorkspaceTabWidget - YAML schema reference"
type: schema-reference
model: WorkspaceTabWidget
is_root: false
keywords: [workspacetabwidget, accent, data, density, icon, id, title, tree]
---

# WorkspaceTabWidget

## Description
Z3 - one tab in the workspace 'Widgets' container.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `id` | str | ✓ | - |  |
| `title` | str | ✓ | - |  |
| `icon` | str \| null |  | `None` |  |
| `accent` | str \| null |  | `None` |  |
| `density` | str \| null |  | `None` |  |
| `data` | dict[str, any] |  | `{}` |  |
| `tree` | [WidgetNode](WidgetNode.md) | ✓ | - |  |

## Linked models
- [WidgetNode](WidgetNode.md)

## Strictness
- `extra: forbid` - unknown keys cause a validation error
