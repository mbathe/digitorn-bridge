---
id: widgets
title: "Widgets — declarative UI rendered by the Flutter client"
type: concept
keywords: [widget, ui, declarative, form, list, table, chart, button, modal, sidebar, workspace, tree, tabs, react, flutter, render, sse, builder, rag, state, template, expression, substitution, validation, stream, upload, per_session, variable, state_bus]
related: [package, agents, bundle-namespaces, preview-module, builder-state-machine]
source: docs/app-language/42-widgets.md
---

# Widgets — declarative UI for app outputs

## What it is

Apps describe rich UIs in YAML and the Flutter client renders them
**without any per-app frontend code**. Every value the user touches
(forms, selections, tool results) becomes a session variable the
agent sees in its system prompt on the next turn. This makes
widgets the canonical bidirectional bus between the UI and the
agent.

## Four layers

1. **Compile-time** — the ``widgets:`` YAML block is validated
   against a 43-primitive / 15-action closed set. External
   ``./widgets/*.yaml`` files are auto-loaded into
   ``widgets.inline`` (file stem = key). Errors carry the exact
   YAML path.
2. **Runtime — agent side** — the agent calls ``widget.render``,
   ``widget.update``, ``widget.close``, ``widget.error``,
   ``widget.set_state``, ``widget.get_state``. Each call goes into
   a per-session store and publishes an SSE delta.
3. **Runtime — daemon side** — the daemon serves the compiled
   tree, resolves data bindings (HTTP/tool/static/local/stream),
   re-validates form values, substitutes ``{{...}}`` templates,
   dispatches user actions, stores ephemeral workspace tabs,
   accepts file uploads, and streams events to the client.
4. **Runtime — client side** — the Flutter client renders
   primitives, manages local form/state/loop scope, and forwards
   user actions to ``POST /widgets/action``.

## Four zones

| Zone | Where the client renders it | YAML key |
|---|---|---|
| ``inline`` (Z1) | Chat bubble | ``widgets.inline.<name>`` |
| ``chat_side`` (Z2) | Companion side panel | ``widgets.chat_side`` |
| ``workspace`` (Z3) | Workspace tab | ``widgets.workspace_tabs[]`` |
| ``modal`` (Z4) | Pop-up dialog | ``widgets.modals.<name>`` |

## 43 primitives (closed set)

Layout: column, row, card, section, tabs, split, grid, spacer,
divider. Content: markdown, text, image, icon. Data display:
list, table, chart, stat, timeline, tree, kanban. Input: form,
text_input, textarea, select, multi_select, radio, checkbox,
switch, slider, date, time, datetime, file_upload, code_editor.
Action: button, icon_button, link, confirm. Feedback: alert,
badge, progress, skeleton, empty_state.

## 15 actions (closed set)

``chat``, ``tool``, ``http``, ``open_url``, ``open_workspace``,
``open_modal``, ``close``, ``set_state``, ``refresh``, ``copy``,
``download``, ``navigate``, ``confirm``, ``sequence``, ``alert``.

## Expression language

Supports dotted-path lookup (``{{a.b.c}}``), indexing
(``{{list[0]}}``), filter pipelines (``{{x | upper | truncate(40)}}``),
comparisons (``{{count > 0}}``), logic (``{{a && b}}``), ternary
(``{{x ? 'yes' : 'no'}}``), ``is empty`` / ``is not empty``,
literals (strings, numbers, bool, null). 24 built-in filters:
upper, lower, title, truncate, default, length, date, relative_time,
money, number, percent, json, filter, map, pluck, join, first,
last, sort, reverse, slice, replace, markdown, plus_days /
minus_days.

## Data sources (5 types)

- ``http``   — HTTP GET/POST relative to the daemon
- ``tool``   — invoke an agent tool with args
- ``static`` — verbatim value
- ``local``  — client-side storage, returns declared default
- ``stream`` — daemon SSE bridge proxying an upstream URL

Resolved via ``GET /api/apps/{id}/widgets/data/{binding}``
(snapshot) and ``GET /widgets/data/{binding}/stream`` (live).

## State as variables (⭐)

Every value the user or the agent writes lands in the session's
widget state map:

| Source | Stored under |
|---|---|
| Form submit (``body.form`` from POST ``/widgets/action``) | ``state.form.<field>`` + ``state.last_form`` |
| ``action: set_state`` | ``state.<key>`` |
| Widget tool result | ``state.results.<tool>`` + ``state.last_result`` |
| Agent ``widget.set_state`` | ``state.<key>`` |
| File uploads | ``state.uploads[file_id]`` |

The daemon auto-injects a ``# WIDGET CONTEXT`` section into the
agent's system prompt every turn, so all these values are visible
without any templating. Tools can also read them explicitly via
``widget.get_state(key="...")``.

## Server-side runtime guarantees

- **Template substitution**: ``widget.render(tree)`` and
  ``widget.update(patch)`` walk every string leaf and substitute
  ``{{form.X}}``, ``{{state.X}}``, ``{{ctx.X}}``, ``{{item.X}}``
  against the live session state **before** emitting the SSE
  event. The client gets concrete values.
- **Form re-validation**: ``POST /widgets/action`` with a
  non-empty ``body.form`` re-runs the client's rules (required,
  regex, min/max, type_hint, multi_select.max, checkbox.required)
  against the compiled tree and rejects 400 with structured
  ``{fields: {name: msg}}`` if any fail.
- **Stream bridge**: ``GET /widgets/data/{binding}/stream`` auto-
  detects SSE upstream (pass-through) vs JSON upstream (HTTP
  poll). Sends a ``meta`` frame first with ``reducer`` + ``limit``.
- **Ephemeral workspace**: ``action: open_workspace`` with
  ``ephemeral:`` stores the tab in the session store so the next
  snapshot includes it.
- **File uploads**: ``POST /widgets/upload`` accepts multipart,
  saves to ``~/.local/share/digitorn/uploads/{user}/{sid}/{id}/``,
  returns ``{file_id, url, size}``, promotes ``file_id`` into
  ``state.uploads``. Per-user scoped downloads.
- **Form → tool auto-merge**: form fields are automatically
  merged into the tool args for actions where ``args:`` is
  omitted.

## Per-session isolation

Each ``session_id`` has its own ``WidgetSessionState`` (state map,
mounted widgets, event ring buffer, subscriber queue). Two users
in parallel **never cross-talk**.

## REST API surface

| Method | Path | Purpose |
|---|---|---|
| ``GET`` | ``/widgets`` | Full compiled tree |
| ``GET`` | ``/widgets/validate`` | Lint |
| ``GET`` | ``/widgets/data/{binding}`` | Resolve binding |
| ``GET`` | ``/widgets/data/{binding}/stream`` | SSE bridge |
| ``POST`` | ``/widgets/action`` | Dispatch |
| ``POST`` | ``/widgets/upload`` | Multipart upload |
| ``GET`` | ``/widgets/upload/.../{filename}`` | Serve back |
| ``GET`` | ``/sessions/{sid}/widget-events`` | SSE stream |

## Wiring an app

```yaml
modules:
  widget: {}

capabilities:
  grant:
    - module: widget
      actions: [render, update, close, error, set_state, get_state]

widgets:
  version: 1
  inline:
    confirm_delete:
      tree:
        type: confirm
        text: "Delete?"
        confirm_action: { action: tool, tool: delete_thing }
```

Then the agent pushes:

```python
await widget.render(
    zone="inline",
    ref="confirm_delete",
    ctx={"path": "/docs/spec.md"},
)
```

For non-trivial apps, drop one widget per file under
``./widgets/*.yaml``. See ``docs/app-language/42-widgets.md`` for
the canonical spec (1700 lines, full primitive and action
reference + integration patterns).

## When to use

- Builder agents that need forms / live canvases
- Workflow editors with visual graphs
- Dashboard apps with live metrics
- Any app where the agent needs to surface structured UI
- RAG apps where the user picks sources and the agent reads them
  live from ``state.selected_sources``

## When NOT to use

- Pure chat apps with no visual state
- Apps that need primitives outside the 43 (embed a full React
  ``preview:`` block instead)
- Apps where the UI rendering logic must live server-side
  (widgets are rendered by Flutter; the daemon validates and
  transports)
