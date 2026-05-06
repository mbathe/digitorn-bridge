# Preview Architecture - Generic Live App Pattern

This document captures the architecture for building "live AI app shells"
(canvas builders, slide makers, code sandboxes, document editors) that all
share the same backend substrate.

## The pattern in one sentence

> A small static React shell + a generic preview module that streams arbitrary
> resources via Socket.IO WebSocket + an agent that pushes data through the
> workspace module into named channels.

No process per session. No process per app at runtime. Scales to thousands of
concurrent users on a single daemon.

## The three layers

### 1. The preview module (server - internal transport)

`modules/preview/module.py`. ALL 17 actions are `internal=True` - invisible
to LLM agents. The workspace module calls them as Python methods. Generic
primitives, all per-session, all publishing Socket.IO deltas the moment they
fire:

```
state operations (key/value scalar map):
  set_state(key, value)
  patch_state(patch)
  get_state()
  clear()

resource operations (named channels of payloads):
  set_resource(channel, id, payload)
  patch_resource(channel, id, patch)
  delete_resource(channel, id)
  list_resources(channel)
  bulk_set_resources(channel, items, replace=False)
  clear_channel(channel)

events (fire-and-forget):
  emit(event_type, data)
```

The module never inspects payloads. A "node" in a canvas, a "slide" in a
deck, a "file" in a code sandbox, a "cell" in a spreadsheet - they're all
just JSON dicts living in `state.resources[channel][id]`.

Backwards compat helpers (`push_node`, `push_edge`, `update_node`,
`highlight_node`, `remove_node`, `push_edge`, `remove_edge`) are thin
wrappers over `set_resource("nodes"|"edges", ...)` so digitorn-builder
keeps working unchanged.

### 2. The workspace module (agent-facing file API)

`modules/workspace/module.py` - 6 actions exposed to agents:
`write`, `read`, `edit`, `glob`, `grep`, `delete`. Tool names: **WsWrite**,
**WsRead**, **WsEdit**, **WsGlob**, **WsGrep**, **WsDelete**.

The agent uses the same API pattern as filesystem - it doesn't know files
live in memory. Under the hood every mutation calls
`preview.set_resource("files", ...)`, streaming changes to the client in
real time. The agent never calls preview directly.

#### Workspace config (app.yaml)

```yaml
modules:
  workspace:
    config:
      render_mode: react      # react | builder | latex | slides | html | markdown | code | auto
      entry_file: src/App.tsx  # main file for the client to render first
      title: My App
      sync_to_disk: false      # mirror writes to real filesystem
      sync_path: null          # disk dir (see resolution order below)
      lint: true               # run diagnostics on every write/edit
      instructions: |          # prepended to all workspace tool prompts
        You are building a React app...
      tool_instructions:       # per-tool override (keys: write, read, edit, glob, grep, delete)
        write: "Custom write instructions..."
```
#### Workspace params - minimal visible, powerful hidden

| Action | Visible params | Hidden params |
|--------|---------------|---------------|
| write  | path, content | - |
| read   | path | offset, limit |
| edit   | path, old_string, new_string | replace_all, insert_at_line, fuzzy_threshold, max_suggestions |
| glob   | pattern | sort_by |
| grep   | pattern | glob, case_insensitive, multiline, before, after, max_results |
| delete | path | - |

#### sync_to_disk - session-isolated by default

When `sync_to_disk: true`, every workspace mutation is mirrored to disk.
The sync directory is resolved in this order:

1. **`sync_path` from YAML** - fixed path, never overridden
2. **`ctx.workspace`** - user-selected folder (passed at session creation)
3. **Auto-isolated per session** - `~/.digitorn/workspaces/{app_id}/{session_id}/`

This means that if no `sync_path` is set in YAML and no user-selected
workspace is provided, each session gets its own isolated directory. No
cross-session file mutation.

Operations with sync_to_disk:
- `write` / `edit` -> writes updated content to `{sync_dir}/{path}`
- `delete` -> removes file from disk
- `read` -> **read-through**: if file not in memory but exists on disk, loads it
- `glob` / `grep` -> scans disk for files not yet loaded, then searches all

#### lint - built-in diagnostics on write/edit

When `lint: true` (default), every `write` and `edit` returns diagnostics
inline in the tool response. Resolution order:
1. **LSP module** (if loaded): `lsp.notify_change(path, content)` -> real
   language server (texlab, pyright, ruff, eslint, etc.)
2. **Built-in content validators**: JSON, YAML, TOML, Python syntax, LaTeX
   (unmatched braces + environments) - work in-memory, no external tools

### 3. The browser SDK (`@digitorn/preview-sdk`)

Published as the `@digitorn/preview-sdk` npm package, located at
`packages/digitorn-preview-sdk/`. Apps declare it as a dependency - it is
NOT copied into each app.

```bash
npm install @digitorn/preview-sdk
```

#### Connection: Socket.IO WebSocket

The SDK connects to the daemon via **Socket.IO** on the `/events` namespace.
Critical rules (hard-learned):

- **Transport must be `["websocket"]` only** - polling returns 400 on auth
- **Token must be in the URL query param** - browser WebSocket does not
  support custom headers (`extraHeaders` crashes in browsers)
- **The daemon auto-adds its own origin to CORS** so previews served from
  the same host never get blocked

Connection code (internal, hidden from app developers):

```typescript
const socket = io(`${baseUrl}/events?token=${encodeURIComponent(token)}`, {
  transports: ["websocket"],
  auth: token ? { token } : {},
  forceNew: true,
  reconnectionDelay: 500,
  reconnectionDelayMax: 10_000,
});
```

On connect, the SDK emits `join_session` with `{app_id, session_id, since}`
to subscribe to that session's event stream. The server replays missed
events since the given sequence number.

#### Provider component

```tsx
import { DigiPreview } from "@digitorn/preview-sdk";

function main() {
  createRoot(document.getElementById("root")!).render(
    <DigiPreview>
      <App />
    </DigiPreview>
  );
}
```

`<DigiPreview>` owns the Socket.IO connection, manages reconnection, and
provides React context to all hooks. It auto-reads session info (appId,
sessionId, token, baseUrl) from the URL query params and path.

Optional props:
- `session` - override session info (useful for testing / Storybook)
- `maxReconnectMs` - max reconnection delay (default 10000)

#### Hooks

| Hook | Returns | Description |
|------|---------|-------------|
| `useConnection()` | `boolean` | true when Socket.IO is connected |
| `useResources<T>(channel)` | `Map<string, T>` | All resources in a channel |
| `useResource<T>(channel, id)` | `T \| undefined` | Single resource by id |
| `usePreviewState<T>(key, default?)` | `T \| undefined` | Single state value |
| `useFiles()` | `Map<string, WorkspaceFile>` | All workspace files |
| `useFile(path)` | `string \| undefined` | Raw content of a single file |
| `useFileJson<T>(path)` | `T \| undefined` | Parse a single JSON file |
| `useFilesByPrefix(prefix)` | `WorkspaceFile[]` | Files matching prefix, sorted |
| `useFilesJsonByPrefix<T>(prefix)` | `{path, data}[]` | Parse all JSON files under prefix |
| `useFileStats()` | `FileStats` | Global file change stats: `{fileCount, added, modified, deleted, totalInsertions, totalDeletions}` |
| `useNodes()` | `PreviewNode[]` | Canvas nodes sorted by updated_at |
| `useEdges()` | `PreviewEdge[]` | Canvas edges |
| `useAgentStatus()` | `AgentStatus` | idle / thinking / working / done / error |
| `useAgentStream()` | `string` | Accumulated text of current agent turn |
| `useToolCalls()` | `ToolCall[]` | Last 50 tool calls |
| `useApprovalRequest()` | `ApprovalRequest \| null` | Non-null when agent awaits confirmation |
| `useEvents(filter?)` | `PreviewEvent[]` | Raw event log (last 100) |

#### Reducer internals

The reducer handles snapshot replay on (re)connect, server-sent deltas
(`resource_set`, `resource_patched`, `resource_deleted`, `channel_cleared`,
`resource_bulk_set`, `state_changed`, `state_patched`, `cleared`) and
maintains a Map per channel for O(1) reads. Agent events (`token`,
`thinking`, `tool_start`, `tool_call`, `turn_complete`, `abort`,
`approval_request`) update agent status and tool call history.

Event log is capped at 500 entries. Tool call history at 50 entries.

## Three preview modes (current)

The previous architecture coupled preview lifecycle to the daemon
(it spawned dev servers at deploy time via `PreviewManager`). That
is **deprecated**. The current model has three modes, all
session-scoped, with the LLM owning lifecycle:

| Mode | Trigger | What runs | Cost / session | Use case |
|---|---|---|---|---|
| `dev_server` (`PreviewProxy`) | LLM calls `Bash(run_in_background)` to spawn a dev server, then `PreviewProxy(port=N)` | Agent's spawned process; daemon proxies HTTP | ~150 MB per attached session | Live coding with HMR |
| `static` (`PreviewStatic`) | LLM calls `Bash("npm run build")`, then `PreviewStatic(path="dist")` | Daemon reads files from disk per request | 0 process | Built-and-served, no HMR |
| `declarative` | App pre-ships `web/dist/index.html`; no LLM action | Daemon reads files from disk per request | 0 process | Pre-built shells (e.g. sandbox apps that bundle in-browser) |

The proxy route `_proxy_preview_http` resolves in this order:

1. `web_preview` registry lookup by `(session_id, name)` →
   serve via the attachment (proxy-to-port or static-from-workspace).
2. Fall-through to `_try_serve_static_dist` which checks the package
   install dir for a built `web/dist/`. If found, it streams the
   file (declarative case).
3. `404` with a hint pointing at `PreviewProxy` / `PreviewStatic`.

The historical `_proxy_preview_http` "dev_server vs static at deploy
time" decision is gone — there is no daemon-side lifecycle anymore.
See [`app-language/41-preview.md`](../../language/41-preview.md) for
the canonical spec.

## Where files actually live (two paths, important)

Builtin apps have **two storage locations**:

- `~/.digitorn/packages/<app_id>/` - the **package install dir** (registered
  in the `installed_packages` table). Holds the canonical source tree
  including `preview/` and `preview/dist/`.
- `~/.digitorn/apps/<app_id>/bundle-<hash>/` - the **bundle dir** (registered
  in the `app_bundles` table). Holds only `app.yaml` + `meta.json`. The
  daemon's `reload_from_db` reads from here.

Patching a builtin's `app.yaml` requires editing BOTH locations, OR running
the upgrade flow that copies source -> install dir -> bundle. The
`current_bundle_id` in `applications` points to the active bundle row.

## Module config YAML - the `config:` wrapper trap

`ModuleBlock` (Pydantic) only knows 4 top-level fields under a module entry:
`config`, `setup`, `constraints`, `middleware`. Anything else is silently
dropped. So this is wrong:

```yaml
modules:
  rag:
    backend:                 # <- never reaches the module
      type: qdrant
      path: "..."
```
Correct form:

```yaml
modules:
  rag:
    config:
      backend:
        type: qdrant
        path: "./.digitorn/knowledge_base/.qdrant"
```
Without the wrapper, `compiled.modules["rag"].config = {}`, the bootstrap
sees `if config:` as falsy, and `module.on_config_update(config)` is never
called.

## Daemon survival rules

Established invariants for the daemon process:

1. **Boot never blocks**. `bootstrap_builtins` is dispatched via
   `asyncio.create_task` so the lifespan returns immediately. Per-package
   timeout 60s, global 600s. Failed builtins are marked BROKEN, daemon stays
   up.

2. **No rename swap on Windows**. `InstallFlow.upgrade` calls
   `_patch_in_place(src, dst)` which walks the source tree with `os.walk`,
   skips preserved dirs (`node_modules`, `dist`, `.vite`, `.next`, ...),
   and overwrites individual files via `shutil.copy2`. Runs in
   `asyncio.to_thread` with 20s timeout. Never renames the install dir
   itself, so no directory handle is required.

3. **Children die with the parent**. `core/process_group.py::install()`
   creates a Windows Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) on
   Windows, calls `setpgrp` on Unix, and applies `PR_SET_PDEATHSIG=SIGKILL`
   on Linux child processes via `set_pdeathsig_on_child(popen_kwargs)`. When
   the daemon dies for any reason - Ctrl+C, kill, terminal close, crash,
   `taskkill /F` - every Vite, npm, sandbox child is terminated by the OS.

4. **Bundle is preserved across upgrades**. `compute_package_hash` excludes
   `node_modules`, `dist`, `build`, `.vite`, `.next`, `.turbo`, `.cache`,
   `__pycache__`, `.output`, `.svelte-kit`, `.digitorn`. Generated artifacts
   never trigger an upgrade.

5. **CORS auto-configuration**. The daemon automatically adds its own origin
   (`http://{host}:{port}`) to the CORS allowed origins list. Previews
   served from the same daemon never get blocked by CORS.

## Building a new live app - recipe

Want to ship "digitorn-slidemaker" or "digitorn-spreadsheet" or "digitorn-mindmap"?

1. **Source tree**:
   ```
   packages/digitorn/builtins/digitorn-<id>/
   ├── app.yaml          # agent + modules: workspace + preview + maybe memory
   ├── package.json      # description metadata only
   └── preview/
       ├── package.json  # react, react-dom, vite, @digitorn/preview-sdk
       ├── vite.config.ts (base = "/api/apps/digitorn-<id>/preview-server/proxy/")
       ├── index.html
       └── src/
           ├── main.tsx        # ReactDOM.createRoot + <DigiPreview><App /></DigiPreview>
           ├── App.tsx
           └── components/...  # your shell components
   ```

2. **Install the SDK**:
   ```bash
   cd packages/digitorn/builtins/digitorn-myapp/preview
   npm install @digitorn/preview-sdk react react-dom
   ```

3. **main.tsx**:
   ```tsx
   import { createRoot } from "react-dom/client";
   import { DigiPreview } from "@digitorn/preview-sdk";
   import { App } from "./App";

   createRoot(document.getElementById("root")!).render(
     <DigiPreview>
       <App />
     </DigiPreview>
   );
   ```

4. **App.tsx** (example for a slide maker):
   ```tsx
   import { useResources, usePreviewState, useAgentStatus } from "@digitorn/preview-sdk";

   export function App() {
     const slides = useResources<Slide>("slides");
     const currentSlide = usePreviewState<string>("current_slide");
     const status = useAgentStatus();

     return (
       <div>
         {status === "working" && <Spinner />}
         {currentSlide && <SlideView slide={slides.get(currentSlide)} />}
       </div>
     );
   }
   ```

5. **app.yaml essentials**:
   ```yaml
   name: My App
   app_id: digitorn-myapp
   version: 0.1.0
   modules:
     preview: {}
     workspace:
       config:
         render_mode: slides
         entry_file: null
         sync_to_disk: false
     memory:
       config:
         working_memory: true
   agents:
     - id: main
       brain:
         provider: anthropic
         model: claude-sonnet-4-5
         config:
           api_key: "{{secret.ANTHROPIC_API_KEY}}"
       system_prompt: |
         You are SlideMaker. Build presentation slides.
         Use WsWrite to create slide files in the "slides" channel.
   capabilities:
     grant:
       - module: workspace
         actions: [write, read, edit, glob, grep, delete]
   preview:
     enabled: false
     command: [npm, run, dev]
     cwd: ./preview
     port: 5176
   ```

6. **Build the static bundle once**:
   ```bash
   cd packages/digitorn/builtins/digitorn-myapp/preview
   npm install
   npx vite build
   ```

7. **Restart the daemon**. Bootstrap deploys the app. Since
   `preview.enabled: false` + `preview/dist/index.html` exists, the daemon
   serves the static shell from `~/.digitorn/packages/digitorn-myapp/preview/dist/`
   via `FileResponse`. `/preview-server/status` reports `mode: static` +
   `state: running`, Flutter displays the iframe.

8. **Test**: open the app, send a message. The agent uses WsWrite/WsEdit to
   push resources through the workspace module. Each mutation internally
   calls `preview.set_resource`, which streams via Socket.IO to the iframe's
   React shell. The shell re-renders. Live preview, zero process per session.

## Data flow diagram

```
User message
  -> Agent loop (agent_loop.py)
    -> Agent calls WsWrite("slides/1.json", content)
      -> workspace.write()
        -> Stores in memory
        -> preview.set_resource("files", "slides/1.json", {content, language, size, lines})
          -> Socket.IO emit to /events namespace
            -> SDK reducer: resource_set -> updates Map
              -> useFiles() / useResources("files") re-render
```

## Real-world examples

| App | Channels used | Agent tools | Effort |
|---|---|---|---|
| **digitorn-builder** (existing) | `nodes`, `edges`; state `current_state`, `yaml_draft` | WsWrite, WsRead, WsEdit | Original reference |
| **digitorn-react-sandbox** (existing) | `files`; state `entry_file`, `errors` | WsWrite, WsRead, WsEdit | Generated React code compiled by esbuild-wasm in the browser |
| **digitorn-slidemaker** (proposed) | `slides`; state `current_slide`, `theme` | WsWrite, WsEdit | ~1 afternoon, no new module |
| **digitorn-word** (proposed) | state `document` (rich text JSON) | WsWrite | ~1 day, no new module |
| **digitorn-excel** (proposed) | `cells` (id="A1"); state `active_sheet` | WsWrite, WsEdit | ~1 day, no new module |
| **digitorn-kanban** (proposed) | `cards`, `columns` | WsWrite, WsEdit, WsDelete | ~half day, no new module |
| **digitorn-mindmap** (proposed) | `nodes`, `edges` (reuse builder shell idea) | WsWrite, WsEdit | ~half day, no new module |
| **digitorn-dashboard** (proposed) | `widgets` | WsWrite, WsEdit, WsDelete | ~1 day, no new module |

Every one of these scales to thousands of concurrent users on a single
daemon because the per-session cost is just JSON in memory + a single
Socket.IO room. No process spawn. No bundle build per session. No per-user
file system mutation (unless sync_to_disk is on, in which case each session
gets its own isolated directory). The state lives in `PreviewSessionState`,
gets evicted on session close, and the React shell is shared across every user.
