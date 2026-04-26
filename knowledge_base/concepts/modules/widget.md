---
id: module-concept-widget
title: "widget module — overview"
type: module-concept
module: widget
isolation: shared
keywords: [widget, widget-module, render, update, close, error, get_state, set_state, clear]
version: 1.0.0
---

# `widget` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `1.0.0`
- **Actions**: 7 visible, 0 internal

## Description (from class docstring)

Widget Module — declarative UI rendered by the Flutter client.

The agent calls ``widget.render(zone, ref/tree, ctx)`` to push a
widget into the user's screen, ``widget.update(widget_id, patch)``
to mutate it live, and ``widget.close(widget_id)`` to take it down.
Each call publishes a Socket.IO event on the per-session widget channel
(namespace ``/events``, room ``session:{session_id}``).

Per-session isolation: every action mutates the state for whichever
``session_id`` the agent loop has activated on the module. Two users
opening two sessions each get two independent widget surfaces.

Widgets at startup come from the app's ``widgets:`` block compiled
into ``CompiledApp.widgets``. The agent uses the actions below to
push **inline** widgets (Z1) or trigger registered ones via ``ref:``.

> Class-level summary: Per-session declarative-widget runtime.

## Configuration

Set under `modules.widget.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon. |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `render` | `WidgetRender` |  | low | Render a widget in one of the 4 zones (inline, chat_side, workspace, modal). |
| `update` | `WidgetUpdate` |  | low | Patch a previously rendered widget (data.X, state.X, ctx.X). |
| `close` | `WidgetClose` |  | low | Close (unmount) a previously rendered widget. |
| `error` | `WidgetError` |  | low | Surface an error in a widget (e.g. failed data binding) without closing it. |
| `get_state` | `WidgetGetState` |  | low | Read the session's widget state (or one key via dotted path). |
| `set_state` | `WidgetSetState` |  | low | Write into the session's widget state — visible to the agent and other modules. |
| `clear` | `WidgetClear` |  | low | Clear all mounted widgets and state for the session. |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: widget
      actions: [render, update, close, error, get_state, set_state, clear]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {widget: [render, update, close, error, get_state]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/widget-*.md`.
