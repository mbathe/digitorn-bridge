---
id: workspace-preview
---

# Workspace & Preview

Two complementary surfaces for apps that produce visible artifacts:

| Block | Purpose | Source |
|-------|---------|--------|
| `ui.workspace` | **Renderer for in-memory virtual files** the agent writes via `WsWrite/Read/Edit/Glob/Grep/Delete`. The client picks the right viewer (React, LaTeX, slides, code, ...) based on `render_mode`. | `schema.py:2717` `WorkspaceBlock` + `modules/workspace/module.py` |
| `ui.preview` | **Spawns a real dev server** (Vite, Next.js, Remix, ...) on deploy and reverse-proxies it through the daemon. The agent doesn't drive it; users open the proxied URL in the client. | `schema.py:2757` `PreviewConfig` |

They can coexist: a Vite app can run as `preview:` while
the agent edits source files via the `workspace` module — every
write triggers HMR through Vite's file watcher.

Every behaviour and field on this page maps to real code; entries
are cited with file + line.

## `ui.workspace` — virtual filesystem renderer

`schema.py:2717` `WorkspaceBlock` (`extra: forbid`). Tells the
client this app uses a virtual file workspace streamed via
Socket.IO. The daemon emits `preview:state_changed` with
`key: "workspace"` on the first file write, carrying these
values so the client picks the right renderer.

```yaml
ui:
  workspace:
    render_mode: react        # auto | react | html | markdown | slides | code | latex | builder
    entry_file: src/App.tsx
    title: "My App"
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `render_mode` | string | `"auto"` | One of: `auto`, `react`, `html`, `markdown`, `slides`, `code`, `latex`, `builder`. `auto` detects from the first file written. |
| `entry_file` | string \| null | `null` | Main file the client opens by default. If omitted, a render-mode-specific default is used. |
| `title` | string \| null | `null` | Optional title shown in the workspace toolbar. |

> **Distinct from `runtime.workdir`.** `ui.workspace` is the
> in-memory virtual filesystem and the renderer hint. `runtime.workdir`
> (`schema.py:2394`) is the physical filesystem path the
> `filesystem`/`shell` modules operate on. The schema renames the
> legacy `execution.workspace` to `runtime.workdir` to remove the
> ambiguity.

### The 6 workspace tools

When `tools.modules.workspace` is loaded, the agent gets six
short-named actions
(`tool_names.py:46-51`):

| Short alias | FQN | Source |
|-------------|-----|--------|
| `WsWrite` | `workspace.write` | `module.py:1383` |
| `WsRead` | `workspace.read` | `module.py:1458` |
| `WsEdit` | `workspace.edit` | `module.py:1529` |
| `WsGlob` | `workspace.glob` | `module.py:1733` |
| `WsGrep` | `workspace.grep` | `module.py:1780` |
| `WsDelete` | `workspace.delete` | `module.py:1869` |

These operate on the **in-memory virtual filesystem** streamed to
the client via Socket.IO — not the real filesystem. The `lint`
field on every `WsWrite` / `WsEdit` response carries fresh
diagnostics from the LSP module
(`module.py:1208` `_run_lint`).

### Auto-detection of `render_mode`

When `render_mode: auto`, the daemon picks
the renderer from the first file's extension:

| Extension | Resolved render_mode |
|-----------|----------------------|
| `.tsx`, `.jsx` | `react` |
| `.tex` | `latex` |
| `.md` (only) | `markdown` |
| `.html` | `html` |
| `slides.md` / `*.slides.md` | `slides` |
| anything else | `code` |

The detection runs once per session at first write and the
`preview:state_changed` event carries the resolved values to the
client.

### Shipping a `workspace` module declaration

```yaml
tools:
  modules:
    workspace:
      config:
        # Module-side config: storage backend, lint toggles, ...
        # See modules/reference/workspace.md for details.

ui:
  workspace:
    render_mode: react
    entry_file: src/App.tsx
    title: "My React app"
```

The `tools.modules.workspace` block enables the WsWrite/Read/...
actions for the agent. The `ui.workspace` block tells the client
how to display the resulting files. Both are needed for a
fully-functional live workspace.

## `ui.preview` — proxied dev server

`schema.py:2757` `PreviewConfig` (`extra: forbid`). For apps that
ship a real Node dev server (Vite, Next.js, Remix, anything that
binds to `localhost:<port>`). The daemon spawns the process on
deploy and reverse-proxies its output through
`/api/apps/<app_id>/preview-server/proxy/...`.

```yaml
ui:
  preview:
    enabled: true
    command: [npm, run, dev]
    cwd: ./web
    port: 5173
    install_command: [npm, install]
    health_path: /
    startup_timeout: 60
    restart_on_crash: true
    env:
      VITE_API_URL: "http://localhost:8000"
      VITE_FEATURE_X: "1"
```

### Fields

`schema.py:2775-2820` (`extra: forbid`).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Disable to skip starting the preview without removing the block. |
| `command` | list[string] | *required* | Command + args to run, e.g. `["npm", "run", "dev"]`. |
| `cwd` | string | `"."` | Working directory relative to the package bundle dir. |
| `port` | int [1024, 65535] | *required* | Port the dev server binds to on localhost. |
| `env` | dict[str, str] | `{}` | Extra environment variables for the preview process. |
| `install_command` | list[string] \| null | `null` | Optional one-time install command (e.g. `["npm", "install"]`). Runs from `cwd` on package install. |
| `health_path` | string | `"/"` | HTTP path polled to detect dev-server readiness. |
| `startup_timeout` | float [1.0, 600.0] | `60.0` | Seconds to wait for the health check before declaring the preview failed. |
| `restart_on_crash` | bool | `true` | Restart the process if it exits unexpectedly (max 3 retries / min). |

### Lifecycle

| Stage | What happens |
|-------|--------------|
| **Package install** | If `install_command` is set, the daemon runs it once in `cwd`. |
| **App deploy** | The daemon spawns `command` in `cwd` with `env` merged on top of the daemon's env. Polls `health_path` until ready (or `startup_timeout` elapses). |
| **Per request** | Inbound requests to `/api/apps/<app_id>/preview-server/proxy/<path>` are forwarded to `localhost:<port><path>`. WebSockets (HMR) are tunneled. |
| **App undeploy** | The dev-server process is terminated cleanly. |
| **Crash recovery** | When `restart_on_crash: true`, the daemon respawns the process up to 3 times per minute. |

### Memory cost

The preview spawns a real Node process per app. Each modern dev
server (Vite + React, Next dev) sits around **~150 MB RAM**.
Don't enable preview on every app — for apps that don't need a
real bundler, use `ui.workspace` (no extra process) instead.

### Static-bundle alternative

When the app's `web/dist/index.html` exists at install time, the
daemon switches to **static mode** automatically:

- No dev server is spawned.
- The static files are served directly from `web/dist/` over the
  same proxy URL.
- Zero process per app.

This is what builtins like `digitorn-builder` use after a
`npm run build` — the canvas runs as a built bundle, not a Vite
dev server. To force dev-server mode while a `dist/` exists, set
`enabled: false` on the static side and keep `preview:` declared.

## When to use which

| Need | Pick |
|------|------|
| Agent writes files; client renders them; no real bundler. | `ui.workspace` only. |
| Agent writes files **and** they need to flow through Vite/Next/... HMR. | `ui.workspace` + `ui.preview`. |
| The app ships a static built site (no agent involvement). | Just `ui.preview` pointed at the built output, OR drop a `web/dist/` next to your YAML and let static mode kick in. |
| The app generates LaTeX / slides / a React mini-app dynamically per session. | `ui.workspace` with the matching `render_mode`. |
| The app is conversation-only (no visible artifacts). | Neither. |

## Cross-references

- App-config block reference (`ui.workspace`, `ui.preview`):
  [App Configuration → ui](02-app-config.md#ui--display-layer-daemon-never-reads)
- Workspace module's 6 actions:
  [Built-in Tools → Workspace tools](04b-builtin-tools.md#workspace-tools-gated-by-toolsmodulesworkspace)
- LSP-driven lint on every workspace write:
  [LSP Diagnostics](27-lsp.md)
- Per-module reference (storage backend, advanced knobs):
  [modules/reference/workspace.md](../modules/reference/workspace.md),
  [modules/reference/preview.md](../modules/reference/preview.md)
- Live frontend SDK (`@digitorn/preview-sdk`) for consuming
  workspace state in a custom client:
  [Client Manifest](44-client-manifest.md)
