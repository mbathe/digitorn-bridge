---
id: widget
title: widget Module
sidebar_label: widget
description: Declarative UI widgets — render / update / close on a per-session Socket.IO room with server-side template substitution.
---

# widget

The **widget** module lets agents push declarative UI
components into the Flutter / web client. The agent calls
`render(zone, ref|tree, ctx)` to mount a widget,
`update(widget_id, patch)` to mutate it live, and
`close(widget_id)` to unmount. Every call publishes a
Socket.IO event on the per-session room which the client
replays into its widget tree with no extra code.

| Property | Value | Source |
|----------|-------|--------|
| Module id | `widget` | `module.py` |
| Action count | 7 (LLM-exposed) | `module.py:352-499` |
| Type | per-app instance, per-session state via `WidgetSessionState` | |
| Transport | Socket.IO (`widget:*` events on `session:{id}` room) | |

## Design

- **Per-session isolation** — each session has its own
  `WidgetSessionState`. Two users in two sessions get two
  independent widget surfaces.
- **Server-side templating** — `{{form.X}}`, `{{state.X}}`,
  `{{ctx.X}}`, `{{item.X}}` tokens inside `tree` or `patch`
  are resolved by `substitute_tree` (`module.py:386-389`)
  **before** publishing — the client renders concrete values.
- **Bidirectional bridge** — widget state is also injected
  into the agent's system prompt under `# WIDGET CONTEXT`,
  so form values, selections, and the last widget-triggered
  tool result are always visible to the next reasoning turn.
- **Compile-time validation** — widget trees are validated by
  the compiler against the closed primitive / action / accent
  / filter sets (`schema.py:2840-2911`).

## Configuration

The widget module has **no user-facing config fields** — all
content lives in the `ui.widgets:` block of `app.yaml`. The
module body just enables it:

```yaml
tools:
  modules:
    widget: {}
```

The full `ui.widgets:` reference (zones, primitives, actions,
expression language, REST API) is in
[Widgets](../../app-language/42-widgets.md). Quick recap:

```yaml
ui:
  widgets:
    version: 1
    chat_side: { tree: { ... } }
    workspace_tabs:
      - { id: results, title: "Results", tree: { ... } }
    modals:
      confirm_delete: { tree: { ... } }
    inline:
      booking_form: { data: { ... }, tree: { ... } }
```

External widget files under `widgets/<name>.yaml` in the
bundle are auto-merged into `inline` keyed by file stem
(same pattern as skills).

## Mount zones

| Zone | `target` | Purpose |
|------|----------|---------|
| `inline` | — | Inline widget in the chat flow. |
| `chat_side` | — | Side panel next to the chat. |
| `workspace` | tab id | Named workspace tab. |
| `modal` | modal name | Dismissible overlay. |

## The 7 actions

`module.py:352-499`. All `risk_level: low`,
`tags=[widget, ui]`.

| Action | Source | Visible params | Purpose |
|--------|--------|----------------|---------|
| `widget.render` | `:352` | `zone`, `target?`, `widget_id?`, `ref?`, `tree?`, `ctx={}`, `turn_id?` | Mount a widget. Takes `ref` XOR `tree`. |
| `widget.update` | `:404` | `widget_id`, `patch: dict` | Patch a mounted widget. Dotted paths: `data.X`, `state.X`, `ctx.X`. |
| `widget.close` | `:428` | `widget_id` | Unmount. |
| `widget.error` | `:443` | `widget_id`, `binding?`, `message` | Surface a binding error without unmounting. |
| `widget.get_state` | `:458` | `key?` (dotted path) | Read state. Returns `{value, found}`. |
| `widget.set_state` | `:477` | `set: dict` | Merge into session state. |
| `widget.clear` | `:489` | — | Unmount everything + wipe state. |

All return `ActionResult(success, data, error)`. `render`
returns `{widget_id}` (auto-generated as `w_<uuid12>` when
not supplied). `update` and `close` echo the `widget_id`.

### Template substitution scope

Built from the live session by `_build_scopes`:

| Scope | Source |
|-------|--------|
| `form.*` | Values typed by the user via widget forms. |
| `state.*` | Everything in `sess.state` except `form`. |
| `ctx.*` | The `ctx` bag the agent passed to `render`. |
| `item.*` | Current item (in list widgets). |
| `session.session_id` | Session id. |
| `app.*` | App id (where supported). |

```python
await widget.update(UpdateParams(
    widget_id=wid,
    patch={"state.greeting": "Hello {{form.name}}"},
))
```

## Socket.IO event types

Every mutation appends a `WidgetEvent` to the session's ring
buffer with an incrementing `seq`, then publishes:

```json
{ "type": "widget:<event_type>", "data": { ...payload, "widget_seq": 17 } }
```

| Event | Emitted by | Payload |
|-------|------------|---------|
| `widget:render` | `render` | Full `MountedWidget.to_dict()` (widget_id, zone, target, ref, tree, ctx, turn_id). |
| `widget:update` | `update` | `{widget_id, patch}` (patch already substituted). |
| `widget:close` | `close` | `{widget_id, was_mounted}`. |
| `widget:error` | `error` | `{widget_id, binding, message}`. |
| `widget:state` | `set_state` | `{state}` (full snapshot post-merge). |
| `widget:cleared` | `clear` | `{}`. |
| `widget:snapshot` | Server → client on `join_session` | Full `WidgetSessionState.snapshot()`. |

Clients use `widget_seq` to reconcile after a reconnect:
request snapshot, then drop live events with
`widget_seq <= snapshot.seq`.

## Prompt injection — `# WIDGET CONTEXT`

`get_prompt_sections()` injects the current session's widget
state every turn:

```markdown
# WIDGET CONTEXT

## Form values
- **name**: "Alice"
- **last_booking_topic**: "1:1 with Alice"

## Session state
- **selected_sources**: ["s1", "s2", "s3"]

## Last widget tool result
- **rag.query**: {"hits": 12, "top_score": 0.94}

## Currently mounted widgets
- **w_a1b2c3d4e5f6** (zone=chat_side, ref=booking_form)
```

The agent references these values in subsequent reasoning
and tool calls — widgets are a first-class bidirectional
bridge between the UI and the LLM.

## Integration notes

- **Flutter / web first** — the client is the primary
  renderer. React apps that just need files (no widget tree)
  usually use `workspace` + `preview` directly.
- **No SSE** — transport is Socket.IO only; widget events
  flow through the same bus as preview / channels.
- **Compile-time validation** — tree shapes are checked at
  compile against the closed
  `WIDGET_PRIMITIVES` / `WIDGET_ACTIONS` / `WIDGET_FILTERS`
  / accent / density / colour sets
  (`schema.py:2840-2911`). Runtime `render` calls that pass
  malformed `tree` return `ActionResult(success=False, ...)`
  without publishing.

## Cross-references

- App-config block reference (`ui.widgets`):
  [App Configuration → ui](../../app-language/02-app-config.md#ui--display-layer-daemon-never-reads)
- Full widget reference (43 primitives, 15 actions,
  expression language, REST API):
  [Widgets](../../app-language/42-widgets.md)
- Parallel transport for React-shaped canvases:
  [preview reference](preview.md)
- File API for live apps:
  [workspace reference](workspace.md)
