# Digitorn Builder — Live Canvas

This directory is the web bundle served by the **preview dev server**
that the daemon spawns when `digitorn-builder` is deployed. It renders
an n8n-style live canvas showing the builder agent's progress in
real-time.

## How it wires together

```
┌───────────────────┐          ┌──────────────────────────────┐
│  Flutter client   │──────────│ daemon                        │
│                   │  iframe  │                               │
│ /api/apps/        │──────────│  reverse proxy                │
│  digitorn-builder │          │   /preview-server/proxy/*     │
│  /preview-server  │          │                               │
│  /proxy/          │          │  (spawned subprocess)         │
│  ?session_id=abc  │          │  └── vite @ 127.0.0.1:5174   │
│                   │          │                               │
│                   │   SSE    │  preview module (per-session) │
│                   │──────────│   /sessions/abc/preview-events│
└───────────────────┘          └──────────────────────────────┘
```

Everything is driven by the preview module: the agent calls
`preview.push_node(...)`, `preview.set_state(...)`, `preview.emit(...)`
and the daemon fans the updates out over a per-session SSE stream. The
React SDK in `src/lib/preview-sdk.ts` subscribes to that stream and
pushes deltas into a shared context.

## Preview SDK usage (any web/ folder)

Copy `src/lib/preview-sdk.ts` into your own app's `web/src/lib/` — no
dependency on a published npm package. Then wrap your tree:

```tsx
import { PreviewProvider, usePreviewNodes, usePreviewState } from './lib/preview-sdk';

function Canvas() {
  const nodes = usePreviewNodes();
  const currentPhase = usePreviewState<string>('current_state');
  return <ReactFlow nodes={nodes.map(toRF)} ... />;
}

ReactDOM.createRoot(root).render(
  <PreviewProvider>
    <Canvas />
  </PreviewProvider>
);
```

Hooks:

| Hook | Returns |
|---|---|
| `useSession()` | `{ appId, sessionId, token, baseUrl }` from URL |
| `usePreview()` | `{ state, nodes, edges, events, seq, connected }` |
| `usePreviewState<T>(key, default)` | one state value, updates live |
| `usePreviewNodes()` | sorted `PreviewNode[]` |
| `usePreviewEdges()` | `PreviewEdge[]` |
| `usePreviewEvents(filter?)` | filtered rolling buffer (max 100) |
| `useConnectionStatus()` | `true` when SSE is open |

## Agent-side — how to drive the canvas

In your app's `app.yaml`, grant the preview module:

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

Then the agent can call:

```python
preview.push_node(
    id="state-2",
    type="state",
    label="Interview",
    position={"x": 240, "y": 120},
)
preview.push_edge(
    id="e-0-1",
    source="state-0",
    target="state-1",
)
preview.highlight_node(id="state-2", status="running")
preview.set_state(key="yaml", value="<current yaml>")
preview.emit(event_type="compile_attempt", data={"attempt": 1, "errors": []})
```

Every call is pushed to the browser instantly via SSE — the React tree
re-renders automatically via the shared reducer inside `PreviewProvider`.

## Per-session isolation

The preview module keeps one `PreviewSessionState` per `session_id`, so
two users opening two tabs each see **their own** canvas. The daemon
only spawns one Vite dev server — the session scoping happens inside
the SSE stream, not in the process layer.

## Local development

From this directory:

```bash
npm install
npm run dev      # Vite at http://127.0.0.1:5174 — standalone
```

When running standalone (not behind the daemon proxy), the SDK falls
back to `session_id=_dev_` and `app_id=digitorn-builder` so you can
exercise the UI without the daemon. Connect the agent in a second
window and drive it — events land in the same store and render in
both tabs.
