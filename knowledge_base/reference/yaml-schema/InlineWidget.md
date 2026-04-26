---
id: yaml-schema-inlinewidget
title: "InlineWidget — YAML schema reference"
type: schema-reference
model: InlineWidget
is_root: false
keywords: [inlinewidget, data, tree]
---

# InlineWidget

## Description
Named inline widget — referenceable by ``ref:`` from agent SSE.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `data` | dict[str, any] |  | `{}` |  |
| `tree` | [WidgetNode](WidgetNode.md) | ✓ | — |  |

## Linked models
- [WidgetNode](WidgetNode.md)

## Strictness
- `extra: forbid` — unknown keys cause a validation error
