---
id: yaml-schema-modalwidget
title: "ModalWidget — YAML schema reference"
type: schema-reference
model: ModalWidget
is_root: false
keywords: [modalwidget, data, dismissible, title, tree, width]
---

# ModalWidget

## Description
Z4 — modal pushed by ``action: open_modal``.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `title` | str \| null |  | `None` |  |
| `width` | int \| str |  | `560` | Modal width preset (one of 420\|560\|640\|720\|'full') or px int. |
| `dismissible` | bool |  | `True` |  |
| `data` | dict[str, any] |  | `{}` |  |
| `tree` | [WidgetNode](WidgetNode.md) | ✓ | — |  |

## Linked models
- [WidgetNode](WidgetNode.md)

## Strictness
- `extra: forbid` — unknown keys cause a validation error
