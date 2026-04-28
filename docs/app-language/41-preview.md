# `preview:` - live dev server + canvas

An app can declare a **live preview** that the daemon spawns on deploy
and tears down on undeploy. The preview is a Node dev server (Vite,
Next.js, Remix, anything) whose output is reverse-proxied through
Digitorn and embedded in the Flutter client as an iframe, with a
per-session canvas state pushed in real-time by the agent through the
`preview` module.

```yaml
preview:
  enabled: true
  command: [npm, run, dev]
  cwd: ./web
  port: 5174
  install_command: [npm, install]
  health_path: /
  startup_timeout: 60
  restart_on_crash: true
```
## What it gives you

- A running Node dev server that the daemon owns (spawn on deploy,
  kill on undeploy, auto-restart on crash with a 3-retry budget)
- A reverse-proxied HTTP + WebSocket route so Flutter can iframe
  `…/preview-server/proxy/?session_id=X&token=Y`
- A **per-session** Socket.IO subscription on `/events` namespace (room `session:{sid}`)
  the React app uses to render live state
- HMR that "just works" through the daemon's WebSocket bridge

## Fields

| Field | Type | Required | Default | Purpose |
|---|---|---|---|---|
| `enabled` | bool | no | `true` | Disable without removing the block |
| `command` | `list[str]` | ✓ | - | The spawn command (e.g. `[npm, run, dev]`) |
| `cwd` | string | no | `"."` | Working dir, relative to the bundle |
| `port` | int | ✓ | - | TCP port the dev server binds on localhost |
| `env` | `dict[str,str]` | no | `{}` | Extra env vars for the dev server |
| `install_command` | `list[str]` | no | - | Runs once on first deploy (marker-guarded) |
| `health_path` | string | no | `"/"` | Path polled for readiness |
| `startup_timeout` | float | no | `60.0` | Seconds to wait before declaring failure |
| `restart_on_crash` | bool | no | `true` | Restart on unexpected exit |

## Node runtime

The daemon auto-installs Node v22 LTS under
`~/.local/share/digitorn/runtimes/node-v22.11.0/` the first time it
boots on a machine without a system Node. You can disable this in
air-gapped environments:

```yaml
# ~/.digitorn/config.yaml
server:
  node_auto_install: false
```
Discover via `digitorn doctor`.

## Driving the canvas from the agent

The `preview` module exposes 11 actions your agent calls like any
other tool. Grant them in capabilities:

```yaml
modules:
  preview: {}

capabilities:
  grant:
    - module: preview
      actions:
        - set_state
        - patch_state
        - clear
        - emit
        - push_node
        - update_node
        - highlight_node
        - remove_node
        - push_edge
        - remove_edge
```
See **`preview-module`** in the knowledge base for the full action
reference and typical usage patterns.

## Ship a `web/` folder

Next to `app.yaml`, add:

```
my-app/
├── app.yaml
├── package.toml
└── web/
    ├── package.json       (react + reactflow + vite)
    ├── vite.config.ts
    ├── tsconfig.json
    ├── index.html
    └── src/
        ├── main.tsx
        ├── App.tsx
        └── lib/
            └── preview-sdk.ts   ← copy from digitorn-builder/web/
```

The SDK file (`preview-sdk.ts`) is a dependency-free React module
that provides:

- `<DigiPreview>` - wraps the tree, owns one Socket.IO connection
- `usePreviewNodes()`, `usePreviewEdges()` - canvas data
- `usePreviewState(key)` - watch a single state scalar
- `usePreviewEvents(filter)` - rolling event buffer
- `useConnectionStatus()` - live badge
- `readSession()` - session id + JWT parsed from the iframe URL (not a React hook - call once at module load)

Copy-pasteable. No npm package to publish.

## Reference implementation

`packages/digitorn/builtins/digitorn-builder/web/` - full ReactFlow
canvas, live YAML panel, state timeline, connection badge. Use it as
the starting point for your own preview app.

## Testing a preview locally

```bash
# 1. Deploy the app (daemon spawns the dev server)
curl -X POST http://localhost:8000/api/packages/install \
  -d '{"source_type":"local","source_uri":"/path/to/my-app","accept_permissions":true}'

# 2. Check status
curl http://localhost:8000/api/apps/my-app/preview-server/status

# 3. Open in a browser (standalone, no Flutter)
open http://localhost:8000/api/apps/my-app/preview-server/proxy/

# 4. Get logs
curl http://localhost:8000/api/apps/my-app/preview-server/logs?limit=50

# 5. Restart if something is wrong
curl -X POST http://localhost:8000/api/apps/my-app/preview-server/restart
```

## When NOT to use `preview:`

- Pure chat apps - no visual state to render
- Short one-shot runs - spawning a Node dev server costs a few
  seconds; not worth it for a 5-second task
- Apps without a web UI you want to iframe

If your app doesn't need a live canvas, just omit the block and
Flutter falls back to the standard chat view.
