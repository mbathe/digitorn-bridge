# preview — universal live preview module

Gives any Digitorn app a **per-session live canvas** without writing a
single line of frontend glue. The agent pushes state, canvas nodes,
edges, and free-form events via `preview.*` actions; the app's
`web/` bundle reads them through a per-session SSE stream via the
`preview-sdk.ts` React hooks.

## How it fits together

```
agent tool call         daemon SSE route           React in web/
─────────────────       ─────────────────          ─────────────────
preview.push_node ──▶   /api/apps/{id}/sessions    <PreviewProvider/>
preview.set_state       /{sid}/preview-events  ──▶  usePreviewNodes()
preview.emit            (EventSource, snapshot     usePreviewState()
                         + live deltas)             usePreviewEvents()
```

- **Per-session isolation** — each `session_id` keeps its own canvas
  state. Two users with two tabs see completely independent canvases.
- **Snapshot replay** — reconnects receive the full current state so
  the UI never desyncs.
- **Zero frontend code required** — the stock SDK + ReactFlow in
  `digitorn-builder/web/` is copy-pasteable into any app.

## Actions

| Action | Purpose |
|---|---|
| `set_state(key, value)` | update one scalar in the session state map |
| `patch_state(patch)` | merge a dict into the state map |
| `get_state()` | read current snapshot |
| `clear()` | wipe everything (state + canvas + events) |
| `emit(event_type, data)` | push a free-form event (no state change) |
| `push_node(id, type, label, position, data, status)` | upsert canvas node |
| `update_node(id, updates)` | partial update of an existing node |
| `highlight_node(id, status)` | set status: idle, running, done, error |
| `remove_node(id)` | drop a node — touching edges cascade-drop |
| `push_edge(id, source, target, label, data)` | upsert canvas edge |
| `remove_edge(id)` | drop an edge |

## Wiring up an app

```yaml
modules:
  preview: {}

capabilities:
  grant:
    - module: preview
      actions: [set_state, push_node, push_edge, highlight_node, emit]

preview:
  enabled: true
  command: [npm, run, dev]
  cwd: ./web
  port: 5174
  install_command: [npm, install]
```

See `digitorn-builder/web/` for a fully wired reference implementation
using ReactFlow, a live YAML panel, and a state timeline.
