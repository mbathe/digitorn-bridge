---
id: yaml-schema-chatsidewidget
title: "ChatSideWidget - YAML schema reference"
type: schema-reference
model: ChatSideWidget
is_root: false
keywords: [chatsidewidget, accent, collapsible, data, default_open, density, icon, title, tree, width]
---

# ChatSideWidget

## Description
Z2 - companion side panel rendered next to the chat.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `title` | str \| null |  | `None` |  |
| `icon` | str \| null |  | `None` |  |
| `collapsible` | bool |  | `True` |  |
| `default_open` | bool |  | `True` |  |
| `accent` | str \| null |  | `None` |  |
| `density` | str \| null |  | `None` |  |
| `width` | int |  | `300` |  |
| `data` | dict[str, any] |  | `{}` |  |
| `tree` | [WidgetNode](WidgetNode.md) | ✓ | - |  |

## Linked models
- [WidgetNode](WidgetNode.md)

## Strictness
- `extra: forbid` - unknown keys cause a validation error
