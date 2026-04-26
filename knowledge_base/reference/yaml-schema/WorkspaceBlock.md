---
id: yaml-schema-workspaceblock
title: "WorkspaceBlock — YAML schema reference"
type: schema-reference
model: WorkspaceBlock
is_root: false
keywords: [workspaceblock, entry_file, render_mode, title]
---

# WorkspaceBlock

## Description
Top-level ``workspace:`` block in app.yaml.

Tells the client this app uses a virtual file workspace streamed
via Socket.IO.  The daemon emits ``preview:state_changed`` with
``key: "workspace"`` on the first file write, carrying these values
so the client can pick the correct renderer.

Example YAML::

workspace:
render_mode: react
entry_file: src/App.tsx
title: "My App"

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `render_mode` | str |  | `'auto'` | How the client should render workspace files. Values: react, html, markdown, slides, code, latex, builder, auto. When 'auto', the daemon detects from the first file written. |
| `entry_file` | str \| null |  | `None` | Main file the client opens by default in the preview (e.g. src/App.tsx, index.html, main.tex). If omitted, a render_mode-specific default is used. |
| `title` | str \| null |  | `None` | Optional title shown in the workspace toolbar. |

## Strictness
- `extra: forbid` — unknown keys cause a validation error
