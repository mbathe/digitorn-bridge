---
id: widget
title: Widget Module
sidebar_label: widget
description: Declarative UI widgets rendered by the Flutter client. Per-session render/update/close over Socket.IO with server-side template substitution.
---

# widget

The **widget** module lets agents push declarative UI components into the
user's Flutter client. The agent calls `render(zone, ref|tree, ctx)` to mount
a widget, `update(widget_id, patch)` to mutate it live, and `close(widget_id)`
to unmount. Every call publishes a Socket.IO event on the per-session room
(`session:{session_id}` on namespace `/events`), which the Flutter client
replays into its widget tree with no extra code.

| Property | Value |
|----------|-------|
| **Module ID** | `widget` |
| **Version** | `1.0.0` |
| **Transport** | Socket.IO (`widget:*` events on `session:{id}` room) |
| **Actions exposed to LLM** | 7 |
| **Isolation** | Per-session (`WidgetSessionState`) |

---

## Design

- **Per-session isolation.** Every action mutates the state for whichever
  `session_id` the agent loop has activated. Two users in two sessions get
  two independent widget surfaces.
- **Server-side templating.** `{{form.X}}`, `{{state.X}}`, `{{ctx.X}}`, and
  `{{item.X}}` tokens inside `tree` or `patch` are resolved server-side via
  `substitute_tree` before publishing, so the client renders concrete values.
- **Bidirectional bridge.** Widget state is also injected into the agent's
  system prompt under `# WIDGET CONTEXT`, so form values, selected sources,
  and the last widget-triggered tool result are always visible to the next
  reasoning turn.
- **Compile-time validation.** Widget trees declared in the `widgets:` block
  are validated by the compiler; only the shapes in the Flutter v1 spec are
  accepted.

---

## Configuration

The widget module has **no user-facing config fields**. All content lives in
the top-level `widgets:` block of `app.yaml` (compiled into
`CompiledApp.widgets`) - the module body just enables it:

```yaml
modules:
  widget: {}
```
### Top-level `widgets:` block

From `core/app/schema.py::WidgetsConfig` - mirrors Flutter spec v1:

```yaml
widgets:
  version: 1
  chat_side:             # optional single panel next to chat
    tree: { ... }
  workspace_tabs:        # ordered list of tabs in the workspace
    - id: results
      label: "Results"
      tree: { ... }
    - id: sources
      label: "Sources"
      tree: { ... }
  modals:                # named modals pushed by agent
    confirm_delete:
      dismissible: true
      tree: { ... }
  inline:                # named widgets referenceable by ref:
    booking_form:
      data: {}
      tree: { ... }
```
External widget files under `./widgets/*.yaml` in the bundle are auto-merged
into `inline` (keyed by file stem) - same pattern as skills.

### Zones

Four mount zones accepted by `render.zone`:

| Zone | Purpose | `target` |
|------|---------|----------|
| `inline` | Inline widget in the chat flow | - |
| `chat_side` | Side panel next to the chat | - |
| `workspace` | Named workspace tab | tab id |
| `modal` | Dismissible overlay | modal name |

---

## Actions (7)

| Action | Visible params | Purpose |
|--------|---------------|---------|
| `render` | `zone`, `target?`, `widget_id?`, `ref?`, `tree?`, `ctx={}`, `turn_id?` | Mount or replace a widget (takes `ref` XOR `tree`) |
| `update` | `widget_id`, `patch: dict` | Patch a mounted widget (dotted paths: `data.X`, `state.X`, `ctx.X`) |
| `close` | `widget_id` | Unmount a widget |
| `error` | `widget_id`, `binding?`, `message` | Surface a binding error without unmounting |
| `get_state` | `key?` (dotted path) | Read widget state (or one value); returns `{value, found}` |
| `set_state` | `set: dict` | Merge key/values into session widget state |
| `clear` | - | Unmount everything and wipe state for the session |

All actions return `ActionResult(success, data, error)`. `render` returns
`{widget_id}` (auto-generated if not supplied via `w_<uuid12>`). `update`
and `close` return the `widget_id` echoed back.

### Template substitution

`render.tree` and `update.patch` values are run through `substitute_tree`
with scopes built from the live session:

- `form` - values typed by the user via widget forms
- `state` - everything in `sess.state` except `form`
- `ctx` - the `ctx` bag the agent passed to `render`
- `item` - the current item (for list widgets)
- `session.session_id` - the session id
- `app` - the app id (where supported)

Example:

```python
await widget.update(UpdateParams(
    widget_id=wid,
    patch={"state.greeting": "Hello {{form.name}}"},
))
```

---

## Socket.IO event types

Every mutation appends a `WidgetEvent` to the session's event ring buffer
with an incrementing `seq`, then publishes:

```json
{ "type": "widget:<event_type>", "data": { ...payload, "widget_seq": 17 } }
```

| Event type | Emitted by | Payload |
|------------|------------|---------|
| `widget:render` | `render` | Full `MountedWidget.to_dict()` (widget_id, zone, target, ref, tree, ctx, turn_id) |
| `widget:update` | `update` | `{widget_id, patch}` (patch already substituted) |
| `widget:close` | `close` | `{widget_id}` |
| `widget:error` | `error` | `{widget_id, binding, message}` |
| `widget:state` | `set_state` | `{state}` (full snapshot post-merge) |
| `widget:cleared` | `clear` | `{}` |
| `widget:snapshot` | Server → client on `join_session` | Full `WidgetSessionState.snapshot()` |

Clients use `widget_seq` to reconcile after a reconnect (request snapshot,
then drop stale live events with `widget_seq <= snapshot.seq`).

---

## Prompt injection - `# WIDGET CONTEXT`

`get_prompt_sections()` injects the current session's widget state into the
system prompt every turn:

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

The agent can reference these values in its reasoning and in subsequent
tool calls - widgets function as a first-class bidirectional bridge between
the UI and the LLM.

---

## Integration notes

- **Flutter-first.** The Flutter client is the primary renderer for widgets.
  React apps typically use `workspace` + `preview` directly instead.
- **No SSE.** Transport is Socket.IO only; all widget events go through the
  same bus as preview and channels.
- **No workbench.** Widget trees render straight into the Flutter client;
  there is no intermediate workbench layer.
- **Validation at compile.** Tree shapes are checked against the Flutter v1
  spec when the app is compiled - runtime `render` calls that pass a malformed
  `tree` return `ActionResult(success=False, ...)` without publishing.

---

## Related

- [`preview`](./preview.md) - parallel transport for React canvas UIs
- [`workspace`](./workspace.md) - file API for live apps
- `core/app/schema.py::WidgetsConfig` - top-level `widgets:` schema
- `CLAUDE.md` - widget module section
