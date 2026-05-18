---
id: workspace-preview
---

# Workspace & Preview

Two complementary surfaces for apps that produce visible artifacts:

| Block | Purpose |
|-------|---------|
| `ui.workspace` | **Renderer for in-memory virtual files** the agent writes via `WsWrite/Read/Edit/Glob/Grep/Delete`. The client picks the right viewer (React, LaTeX, slides, code, ...) based on `render_mode`. |
| `tools.modules.web_preview` | **Session-scoped iframe-preview attachments** driven by the LLM at runtime. Two regimes: **proxy** to a dev server the agent spawned, or **static** to a directory the agent built. Replaces the deprecated `ui.preview` declarative block. |

They can coexist: a Vite app can ship `workspace` for the file-edit
loop while the agent (in the same session) calls `PreviewProxy(port=5173)`
to point the user's preview iframe at a live `npm run dev`.

Every behaviour and field on this page maps to real code; entries
are cited with file + line.

## `ui.workspace` - virtual filesystem renderer

`schema.py` `WorkspaceBlock` (`extra: forbid`). Tells the
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
| `position` | string | `"right"` | Layout: `right`, `bottom`, `hidden`, `overlay`. |
| `width_pct` | int [10, 90] | `50` | Workspace pane width as a percentage of the split. Ignored when `position` is `hidden` / `overlay`. |
| `auto_open_on_first_tool` | bool | `true` | Open the pane on the agent's first file write or workbench event. |
| `default_open` | bool | `false` | Open the pane immediately on session mount (Lovable-style; bypasses `auto_open_on_first_tool`). |

> **Distinct from `runtime.workdir`.** `ui.workspace` is the
> in-memory virtual filesystem and the renderer hint. `runtime.workdir`
> (`schema.py`) is the physical filesystem path the
> `filesystem`/`shell` modules operate on. The schema renames the
> legacy `execution.workspace` to `runtime.workdir` to remove the
> ambiguity.

### The 6 workspace tools

When `tools.modules.workspace` is loaded, the agent gets six
short-named actions
(`tool_names.py`):

| Short alias | FQN |
|-------------|-----|
| `WsWrite` | `workspace.write` |
| `WsRead` | `workspace.read` |
| `WsEdit` | `workspace.edit` |
| `WsGlob` | `workspace.glob` |
| `WsGrep` | `workspace.grep` |
| `WsDelete` | `workspace.delete` |

These operate on the **in-memory virtual filesystem** streamed to
the client via Socket.IO - not the real filesystem. The `lint`
field on every `WsWrite` / `WsEdit` response carries fresh
diagnostics from the LSP module
(`module.py` `_run_lint`).

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

## `tools.modules.web_preview` - session-scoped iframe attachments

`modules/web_preview/module.py`. The agent attaches the iframe at
`/api/apps/<app_id>/preview/?session_id=<sid>` to one of:

- **A running dev server** (HTTP proxy) on a TCP port the agent
  spawned itself via `Bash(run_in_background=true)`.
- **A static directory** inside the session workspace, served from
  disk live with no process or port.

Attachments are scoped to `(session_id, name)`, so two sessions
of the same app see independent previews and a single session can
expose multiple previews in parallel (`name="frontend"`,
`name="backend"`, ...). The daemon never spawns dev servers on
its own - every server is LLM-driven, lazy, and torn down when the
session ends.

```yaml
tools:
  modules:
    web_preview: {}            # no config required
    workspace:                 # optional, for the file-edit loop
      config:
        sync_to_disk: true     # required so the build can read agent-written files from disk
```

### The 3 tools the agent gets

| Short alias | FQN | Purpose |
|-------------|-----|---------|
| `PreviewProxy` | `web_preview.proxy` | Proxy the iframe to a running dev server on a port. |
| `PreviewPublish` | `web_preview.publish` | Build the project once and serve the static output same-origin. |
| `PreviewDetach` | `web_preview.detach` | Drop a previously-registered attachment by name. |

### Three preview regimes

| Regime | Trigger | What the agent does | Resource cost |
|--------|---------|---------------------|---------------|
| **Live dev server** | "Build me an app, I want HMR" | `Bash("npm run dev", run_in_background=true)` → wait until bound → `PreviewProxy(port=5173)` | One Node process per attached session |
| **Built static** | "Build the app, deploy it" | `PreviewPublish()` runs the build + serves `dist/` same-origin | Zero process; daemon serves from disk per request |
| **Declarative ship** | App pre-ships `web/dist/` and the iframe just consumes it | None - fall-through resolution serves `web/dist/` automatically | Zero process |

### Routing resolution order

`/api/apps/<app_id>/preview/?session_id=X[&name=Y]`:

1. `web_preview` registry has `(X, Y or "default")` → serve via the
   attachment (proxy to port, or static from workspace dir).
2. The app's install dir contains a `web/dist/index.html` → serve
   the file directly (declarative case, no LLM action required).
3. `404 Not Found`, with a hint pointing at `PreviewProxy` /
   `PreviewPublish`.

### Multi-attach by name

```python
# Frontend dev server + backend dev server, same session.
Bash("cd web && npm run dev", run_in_background=true)
Bash("cd api && uvicorn main:app --port 8001", run_in_background=true)
PreviewProxy(port=5173, name="frontend")
PreviewProxy(port=8001, name="backend")
```

The iframe URL picks one via `?name=`:
`/api/apps/<id>/preview/?session_id=X&name=backend`.

### Lifecycle

| Stage | What happens |
|-------|--------------|
| **Daemon boot** | Nothing. No dev servers spawned, no `npm install`. The `web_preview` module just registers its 3 tools. |
| **Session start** | Same: nothing happens preview-side until the LLM acts. |
| **Agent attaches** | `PreviewProxy` / `PreviewPublish` registers `(session_id, name)` in the in-memory registry. Health check on proxy is best-effort. |
| **Per request** | The route looks up the attachment by `(session_id, name)` and serves accordingly. Static reads disk live, so rebuilds are visible on the next page load with no re-attach needed. |
| **Session end** | `cleanup_session(sid)` drops all attachments for that session. Background bash tasks are killed by the shell module's own cleanup. |

### Why the agent owns dev-server lifecycle

The previous `ui.preview` block had the daemon spawn Vite at deploy
time, which couples preview lifecycle to the daemon process and
forces it to deal with port allocation, zombie cleanup, restart
budgets, and concurrent warmups. None of that is the daemon's job
on a session-multiplexed framework - and an agent that writes its
own files is in a far better position to:

- Pick a port (and resolve conflicts itself with a `Bash` kill).
- Wait for the server to actually bind before attaching (read its
  output).
- Decide between dev mode and built-static mode per task.
- Tell the user which mode it picked and where to look.

### Deprecated: `ui.preview` block

The legacy `ui.preview` block (with `command`, `port`, `cwd`,
`install_command`, `startup_timeout`, …) is **deprecated**. The
daemon used to spawn the dev server at deploy time; it now ignores
the block. Migrate by:

1. Remove the `ui.preview` section from your YAML.
2. Add `tools.modules.web_preview: {}`.
3. Add `system_prompt` instructions telling the agent to spawn the
   dev server via `Bash(run_in_background=true)` and call
   `PreviewProxy(port=N)` once it binds.

For apps that simply ship a built `web/dist/`, removing the block
is enough - the routing fall-through serves the static bundle
automatically.

## When to use which

| Need | Pick |
|------|------|
| Agent writes files; client renders them; no real bundler. | `ui.workspace` only. |
| Agent writes files **and** they need to flow through Vite/Next/... HMR. | `ui.workspace` + `tools.modules.web_preview` (agent calls `PreviewProxy` on the dev server it spawned). |
| Agent builds the app and serves the bundle. | `ui.workspace` + `tools.modules.web_preview` (agent calls `PreviewPublish`, which runs the build + serves it same-origin). |
| App pre-ships `web/dist/` and just consumes session state in the iframe. | Neither block - the routing fall-through serves it automatically. |
| The app generates LaTeX / slides / a React mini-app dynamically per session. | `ui.workspace` with the matching `render_mode`. |
| The app is conversation-only (no visible artifacts). | Neither. |

## Cross-references

- App-config block reference (`ui.workspace`):
  [App Configuration → ui](02-app-config.md#ui---display-layer-daemon-never-reads)
- Workspace module's 6 actions:
  [Built-in Tools → Workspace tools](04b-builtin-tools.md#workspace-tools-gated-by-toolsmodulesworkspace)
- `web_preview` tool prompts (system-prompt section + per-action
  guidance the agent receives): `modules/web_preview/module.py`
  `get_prompt_sections` + each `@action(tool_prompt=...)`.
- LSP-driven lint on every workspace write:
  [LSP Diagnostics](27-lsp.md)
- Per-module reference (storage backend, advanced knobs):
  [modules/reference/workspace.md](../reference/modules/workspace.md)
- Live frontend SDK (`@digitorn/preview-sdk`) for consuming
  workspace state in a custom client:
  [Preview SDK](47-preview-sdk.md) - full hooks reference, `<DigiPreview>`
  provider, `useWorkspaceFiles`, hidden namespaces, host ↔ iframe
  protocol, bundled-app auto-attach.
