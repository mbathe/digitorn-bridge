---
id: yaml-schema-widgetsconfig
title: "WidgetsConfig - YAML schema reference"
type: schema-reference
model: WidgetsConfig
is_root: false
keywords: [widgetsconfig, chat_side, inline, modals, version, workspace_tabs]
---

# WidgetsConfig

## Description
Top-level ``widgets:`` block in app.yaml.

Structure mirrors the Flutter spec v1: one optional chat_side
panel, an array of workspace_tabs, a dict of named modals, and a
dict of named inline widgets that the agent can push via
``widget.render`` with a ``ref:``.

External widget files under ``./widgets/*.yaml`` in the bundle
dir are loaded by the compiler and merged into the ``inline``
map (keyed by file stem) - same pattern as skills.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `version` | int |  | `1` | Spec version. Daemon refuses unknown versions. |
| `chat_side` | [ChatSideWidget](ChatSideWidget.md) \| null |  | `None` |  |
| `workspace_tabs` | list[[WorkspaceTabWidget](WorkspaceTabWidget.md)] |  | `[]` |  |
| `modals` | dict[str, [ModalWidget](ModalWidget.md)] |  | `{}` |  |
| `inline` | dict[str, [InlineWidget](InlineWidget.md)] |  | `{}` |  |

## Linked models
- [ChatSideWidget](ChatSideWidget.md)
- [InlineWidget](InlineWidget.md)
- [ModalWidget](ModalWidget.md)
- [WorkspaceTabWidget](WorkspaceTabWidget.md)

## Strictness
- `extra: forbid` - unknown keys cause a validation error
