# widget - Integration Guide

`widget` lets the agent **mount declarative UI components** into the
Flutter / web client without writing any React/TSX code. The agent
references a widget by name (from the app's `widgets:` block in YAML)
and the client renders it in the right zone.

## Actions

| Action | Purpose |
|---|---|
| `render` | Mount a widget in one of 4 zones (`inline`, `chat_side`, `workspace`, `modal`). |
| `update` | Patch an existing mounted widget's props. |
| `close` | Unmount a widget by id. |
| `emit_event` | Fire a named event that a mounted widget is listening for. |
| `list_mounted` | Inspect what's currently on screen. |

## The 4 zones

| Zone | Where it renders |
|---|---|
| `inline` | Inside the current chat bubble (next to the assistant text) |
| `chat_side` | Side panel next to the conversation |
| `workspace` | Main content area (pre-empts any workspace preview) |
| `modal` | Overlay dialog |

## How widgets are declared

The agent can either **reference a named widget** from the app's YAML
`widgets:` block (preferred), or pass an **inline `tree`** for
disposable one-shot UIs:

```yaml
widgets:
  confirm_delete:
    kind: Confirm
    props:
      title: "Delete {{ctx.name}}?"
      body: "This cannot be undone."
      actions:
        - { label: "Delete", tool: filesystem.rm, params: { path: "{{ctx.path}}" } }
        - { label: "Cancel", close: true }
```

Then the agent just calls:

```
widget.render(ref="confirm_delete", ctx={path: "/foo", name: "foo.txt"})
```

## Tool callbacks from widgets

Widgets can trigger tool calls via their `actions[*].tool` fields.
When the user clicks a button, the client POSTs to
`/api/apps/{app_id}/widgets/action` which dispatches the declared tool
call through the normal agent security profile (capabilities, approval
queue, constraints - all applied).

## Constraints

No module-level constraints. Security comes from:
- The declared `widgets:` block (agent can only `ref=` widgets listed there).
- The tool-callback path going through the standard agent security profile.

## Isolation

`widget` is `shared` per app. Each render is keyed by `(session_id,
widget_id)` so two sessions see distinct mounted widgets.

## When NOT to use

- You're streaming free-form text or file changes → use `preview` +
  `workspace` instead (cheaper, no widget schema overhead).
- One-liner confirmations the agent can handle via `AskUser` - that's
  simpler than defining a widget.

## Related

- `docs/WIDGETS_END_TO_END.md` - full walkthrough of the render → action loop
- `modules/preview` - for state / resource streaming (the other UI channel)
