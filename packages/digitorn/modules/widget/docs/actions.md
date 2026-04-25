# widget — actions reference

## render

Mount a widget in one of 4 zones.

```yaml
widget.render:
  zone: inline           # inline | chat_side | workspace | modal
  ref: confirm_delete    # OR tree: { ... } (mutually exclusive)
  ctx: { path: "/foo" }  # exposed as ctx.* in the tree
  widget_id: w_abc       # optional — auto-generated if omitted
  turn_id: t_123         # optional
```

Returns `{widget_id}` — pass it to ``update`` / ``close`` later.

## update

Live-patch a mounted widget. Use dotted paths.

```yaml
widget.update:
  widget_id: w_abc
  patch:
    "data.sources": [...]
    "state.filter": "active"
```

## close

Unmount a widget.

```yaml
widget.close:
  widget_id: w_abc
```

## error

Surface an error WITHOUT closing the widget — useful when a data
binding fails and you want the user to see the error inline.

```yaml
widget.error:
  widget_id: w_abc
  binding: sources
  message: "Backend timeout"
```

## get_state / clear

Inspection helpers — `get_state()` returns the per-session snapshot
(mounted widgets + state map + recent events), `clear()` wipes
everything for this session.
