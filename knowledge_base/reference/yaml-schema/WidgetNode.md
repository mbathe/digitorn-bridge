---
id: yaml-schema-widgetnode
title: "WidgetNode — YAML schema reference"
type: schema-reference
model: WidgetNode
is_root: false
keywords: [widgetnode, accent, as_, body, children, density, empty, first, for_, hidden, id]
---

# WidgetNode

## Description
Recursive widget tree node — every primitive shares this base.

Pydantic refuses extra fields globally, BUT each primitive needs
its own keys (``items`` for list, ``rows`` for table, ``children``
for column/row, etc.). Rather than declare 30 strict subclasses
we use a permissive shape and validate the per-primitive contract
in :func:`digitorn.core.app.compiler._validate_widget_tree`.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `type` | str | ✓ | — | Primitive name — must be in WIDGET_PRIMITIVES. |
| `id` | str \| null |  | `None` |  |
| `when` | str \| null |  | `None` | Conditional render expression. |
| `for_` | str \| null |  | `None` |  |
| `as_` | str \| null |  | `None` |  |
| `key` | str \| null |  | `None` |  |
| `accent` | str \| null |  | `None` |  |
| `density` | str \| null |  | `None` |  |
| `hidden` | bool \| null |  | `None` |  |
| `children` | list[[WidgetNode](WidgetNode.md)] \| null |  | `None` |  |
| `item` | [WidgetNode](WidgetNode.md) \| null |  | `None` |  |
| `first` | [WidgetNode](WidgetNode.md) \| null |  | `None` |  |
| `second` | [WidgetNode](WidgetNode.md) \| null |  | `None` |  |
| `body` | [WidgetNode](WidgetNode.md) \| null |  | `None` |  |
| `render` | [WidgetNode](WidgetNode.md) \| null |  | `None` |  |
| `empty` | [WidgetNode](WidgetNode.md) \| null |  | `None` |  |
| `loading` | [WidgetNode](WidgetNode.md) \| null |  | `None` |  |
| `submit` | dict[str, any] \| null |  | `None` |  |
| `reset` | dict[str, any] \| null |  | `None` |  |

## Strictness
- `extra: forbid` — unknown keys cause a validation error
