# widget - declarative UI runtime

Per-session canvas of UI widgets pushed by the agent and rendered
by the Flutter client. Trees come from the app's ``widgets:`` block
(static) or are emitted live via ``widget.render`` actions (dynamic).

## Actions

| Action | Purpose |
|---|---|
| `render(zone, ref/tree, ctx)` | Mount a widget in inline / chat_side / workspace / modal |
| `update(widget_id, patch)` | Patch data.X / state.X / ctx.X of a mounted widget |
| `close(widget_id)` | Unmount a widget |
| `error(widget_id, message)` | Surface an error inside a mounted widget without closing it |
| `get_state()` | Return full session snapshot for replay |
| `clear()` | Wipe all mounted widgets + state for the session |

## Wire it up

```yaml
modules:
  widget: {}

capabilities:
  grant:
    - module: widget
      actions: [render, update, close, error]

widgets:
  version: 1
  inline:
    confirm_delete:
      tree:
        type: confirm
        text: "Delete?"
        confirm_label: Delete
        destructive: true
        confirm_action: { action: tool, tool: delete_thing }
```

External `./widgets/*.yaml` files are auto-loaded as inline widgets
(file stem becomes the inline key).

See `docs/app-language/42-widgets.md` for the full primitive reference.
