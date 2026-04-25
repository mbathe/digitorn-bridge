# Preview — Live preview system

> **SDK update (v1.1)**: The browser SDK is now the `@digitorn/preview-sdk`
> npm package. **Do not copy `preview-sdk.ts` manually** — install the
> package instead. See `docs/PREVIEW_ARCHITECTURE.md` and
> `knowledge_base/concepts/preview-sdk.md` for the current API.
>
> Key changes:
> - Provider: `<DigiPreview>` (was `<PreviewProvider>`)
> - Connection: Socket.IO WebSocket only (was SSE)
> - Hooks: `useFiles()`, `useFile()`, `useConnection()`, `useAgentStatus()` etc.
> - Workspace isolation: auto per-session (`~/.digitorn/workspaces/{app_id}/{session_id}/`)

This document covers the three pieces that make up Digitorn's live
preview system:

1. **NodeRuntime** — auto-installed Node.js runtime the daemon manages
2. **PreviewManager** — per-app dev server supervisor driven by an
   `app.yaml` block
3. **Preview module + workspace module** — per-session state consumed
   by a React app using `@digitorn/preview-sdk`

Together they let any Digitorn app embed a live preview (code sandbox,
canvas builder, slide maker, document editor) by declaring ~10 lines
of YAML and installing the SDK package.

---

## 1. NodeRuntime

### What it is

A daemon-wide service that resolves and manages a Node.js runtime for:

- Node-based MCP servers (anything spawned by `node` or `npx`)
- App preview dev servers (Vite, Next.js, Remix, anything else that
  uses Node)
- Package install hooks (`install_command: [npm, install]`)
- Future: sandboxed Node workers for user scripts

The runtime is a **singleton** (`get_node_runtime()`) initialised
during the daemon lifespan. Every consumer uses the same instance.

### Resolution order

1. **System PATH** — `node --version` must be ≥ v20
2. **Version managers** — discovers nvm / fnm / volta installations
   and injects their `bin/` dir into the spawn env
3. **Auto-install** — downloads Node v22.11.0 LTS from
   `nodejs.org/dist/` into `~/.local/share/digitorn/runtimes/node-v22.11.0/`
   (or `%APPDATA%\Digitorn\runtimes\` on Windows). Platform-aware:
   `linux-x64`, `linux-arm64`, `darwin-x64`, `darwin-arm64`, `win-x64`.

Auto-install is cached — once extracted, subsequent daemon boots
reuse the install instantly.

### Config

```yaml
# ~/.digitorn/config.yaml
server:
  node_auto_install: true   # default; set to false in air-gapped / CI envs
```
If disabled and no Node is found, the daemon still boots — Node-
dependent features surface a clear error at the point of use.

### API

```python
from digitorn.core.runtime.node_runtime import get_node_runtime

rt = get_node_runtime()
await rt.ensure_installed()           # idempotent
print(rt.node_path, rt.version)       # /path/to/node, "22.11.0"
proc = await rt.spawn("npm", ["install"], cwd="/path/to/web")
rc, stdout, stderr = await rt.run("node", ["-v"], timeout=5.0)

env = rt.env  # os.environ copy with Node's bin prepended to PATH
```

### CLI

```bash
digitorn doctor   # reports Node version + source (system / nvm / auto-install)
digitorn setup    # interactive; offers auto-install if missing
```

---

## 2. PreviewManager + `preview:` YAML block

### What it is

A per-app supervisor that spawns a dev server process at deploy time,
supervises it (restart on crash, 3 retries per 60s), and kills it
cleanly on undeploy. One instance per deployed app, owned by
`DeployedApp.preview_manager`.

### YAML reference

```yaml
preview:
  enabled: true                    # defaults to true; disable without removing the block
  command: [npm, run, dev]         # required
  cwd: ./web                       # working dir, relative to the bundle dir
  port: 5174                       # TCP port the dev server binds on localhost
  env:                             # extra env vars (merged on top of NodeRuntime.env)
    VITE_API_URL: http://localhost:8000
  install_command: [npm, install]  # optional one-shot; idempotent via .digitorn-preview-installed marker
  health_path: /                   # HTTP path probed for readiness
  startup_timeout: 60.0            # seconds to wait; raises TimeoutError on miss
  restart_on_crash: true           # default true
```
### Lifecycle

```
deploy:
  PreviewManager(config, bundle_dir, app_id).install()
  PreviewManager(...).start()
    ├─ spawn command via NodeRuntime.spawn()
    ├─ stream stdout/stderr into a ring buffer (500 lines)
    ├─ poll TCP localhost:port every 0.5s until reachable or timeout
    └─ supervisor task watches process; on unexpected exit, restart

undeploy:
  PreviewManager.stop()
    ├─ cancel supervisor task
    ├─ proc.terminate() + 5s grace
    └─ proc.kill() if still alive
```

### API routes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/apps/{id}/preview-server/status` | state + pid + logs_tail + restart_count |
| `GET` | `/api/apps/{id}/preview-server/logs?limit=200` | ring buffer contents |
| `POST` | `/api/apps/{id}/preview-server/restart` | stop + start, resets crash budget |
| `*` | `/api/apps/{id}/preview-server/proxy/{path}` | HTTP reverse proxy → `localhost:{port}` |
| `WS` | `/api/apps/{id}/preview-server/ws/{path}` | WebSocket bridge for HMR |

The reverse proxy forwards method, headers (minus hop-by-hop),
query string, and body. The WebSocket upgrade bridges frames in both
directions via the `websockets` library.

### Flutter integration

The client embeds an iframe:

```
/api/apps/{app_id}/preview-server/proxy/?session_id={sessionId}&token={jwt}
```

`token` is passed as a query param because iframes cannot set
`Authorization` headers. The daemon accepts it via the standard auth
middleware path.

See `docs/FLUTTER_PREVIEW_WORKSPACE.md` for the full Flutter widget
spec.

---

## 3. Preview module — per-session live canvas

### What it is

A universal Python module that gives agents a set of actions to push
state, resources (nodes, edges, files, slides, anything) and events to a
**per-session** Socket.IO stream. The React SDK in any app's `web/` folder
reads that stream and renders it via whatever UI library the developer
prefers.

**Key property:** per-session isolation. Two users with two sessions each
see two completely independent canvases, but share the same underlying
dev server (or static bundle) process. No per-session process spawn — just
per-session state in the module.

### Actions (17 total, all `internal=True`)

All preview actions are `internal=True` — invisible to LLM agents. The
workspace module calls them as Python methods (`self._preview.set_resource(...)`).

| Action | Purpose |
|---|---|
| `set_state(key, value)` | update one scalar in the session state map |
| `patch_state(patch)` | merge a dict into the state map |
| `get_state()` | return snapshot |
| `clear()` | wipe everything |
| `emit(event_type, data)` | push a free-form event |
| `set_resource(channel, id, payload)` | upsert a resource in a named channel |
| `patch_resource(channel, id, patch)` | shallow-merge a patch into a resource |
| `delete_resource(channel, id)` | remove a resource from a channel |
| `list_resources(channel)` | snapshot of all resources in a channel |
| `bulk_set_resources(channel, items, replace=False)` | upsert many at once (optionally replacing the channel) |
| `clear_channel(channel)` | drop all resources in a channel |
| `push_node(id, type, label, position, data, status)` | upsert a canvas node (wrapper over `set_resource("nodes", ...)`) |
| `update_node(id, updates)` | partial update of a node |
| `highlight_node(id, status)` | set status (idle/running/done/error) |
| `remove_node(id)` | drop a node + cascade-drop touching edges |
| `push_edge(id, source, target, label, data)` | upsert a canvas edge (wrapper over `set_resource("edges", ...)`) |
| `remove_edge(id)` | drop an edge |

Every mutation publishes a `PreviewEvent` with a monotonic `seq`. Clients
dedupe on reconnect by ignoring deltas where `delta.seq <= last_observed_seq`.

### React SDK — `@digitorn/preview-sdk`

Install the SDK package in your app's `web/`:

```bash
npm install @digitorn/preview-sdk
```

Wrap your React tree with `<DigiPreview>`:

```tsx
import { createRoot } from "react-dom/client";
import { DigiPreview } from "@digitorn/preview-sdk";
import { App } from "./App";

createRoot(document.getElementById("root")!).render(
  <DigiPreview>
    <App />
  </DigiPreview>,
);
```

`<DigiPreview>` owns the Socket.IO connection (`/events` namespace),
manages reconnection, and provides React context to all hooks. It
auto-reads session info (appId, sessionId, token, baseUrl) from the
iframe URL's query params.

Hooks (17 total):

| Hook | Returns |
|---|---|
| `useConnection()` | `boolean` — true when Socket.IO is connected |
| `useResources<T>(channel)` | `Map<string, T>` — all resources in a channel |
| `useResource<T>(channel, id)` | `T \| undefined` — a single resource |
| `usePreviewState<T>(key)` | `T \| undefined` — a single state value |
| `useFiles()` | `Map<string, WorkspaceFile>` — all workspace files |
| `useFile(path)` | `string \| undefined` — raw content of one file |
| `useFileJson<T>(path)` | `T \| undefined` — parsed JSON of one file |
| `useFilesByPrefix(prefix)` | `WorkspaceFile[]` — files under a prefix |
| `useFilesJsonByPrefix<T>(prefix)` | `{path, data: T}[]` — parsed JSON files under prefix |
| `useFileStats()` | `FileStats` — `{fileCount, added, modified, deleted, totalInsertions, totalDeletions}` |
| `useNodes()` | `PreviewNode[]` — canvas nodes sorted by `updated_at` |
| `useEdges()` | `PreviewEdge[]` — canvas edges |
| `useAgentStatus()` | `"idle" \| "thinking" \| "working" \| "done" \| "error"` |
| `useAgentStream()` | `string` — accumulated tokens of the current turn |
| `useToolCalls()` | `ToolCall[]` — last 50 tool calls |
| `useApprovalRequest()` | `ApprovalRequest \| null` — non-null when the agent awaits confirmation |
| `useEvents(filter?)` | `PreviewEvent[]` — raw event log (last 100) |

All hooks inside the tree share one Socket.IO connection, one reducer, and
one snapshot. You can compose as many components as you want.

### Driving the canvas from the agent

Grant the actions you need:

```yaml
capabilities:
  grant:
    - module: preview
      actions: [set_state, push_node, push_edge, highlight_node, emit]
```
Then the agent just calls them like any other tool:

```
preview.push_node(id="state-2", type="state", label="Interview", position={x:240, y:120})
preview.push_edge(id="e-1-2", source="state-1", target="state-2")
preview.highlight_node(id="state-2", status="running")
preview.set_state(key="yaml", value="<current yaml>")
preview.emit(event_type="compile_attempt", data={attempt: 1})
```

The browser sees each mutation ~1ms later via the Socket.IO `/events`
namespace.

### Durable snapshot + checkpoint / fork

Every `preview.set_state` / `preview.set_resource` etc. is written
to an in-memory `PreviewSessionStore`. A 500 ms debounced flush
persists the full `(state, resources, seq)` tuple to the
`session_workspace_snapshots` table (one row per `session_id`).
Bursts of mutations coalesce into a single DB write; aborting a
session or shutting down the daemon force-flushes before drop.

On the first access after a daemon restart (either via
`GET /api/apps/{app_id}/sessions/{sid}/workspace` or the Socket.IO
`join_session` handler), the store is rehydrated from DB so the
client sees the exact same canvas it left behind.

Three endpoints expose this as a user-facing feature:

| Method + path | Purpose |
|---|---|
| `GET  …/sessions/{sid}/workspace/export` | Returns a portable `WorkspaceSnapshotEnvelope` (format string, version, state, resources, seq). Used for "Save a copy". |
| `POST …/sessions/{sid}/workspace/import` | Body: `{snapshot, replace}`. Replaces or merges the envelope into the current session, then force-flushes so the change is durable. |
| `POST …/sessions/{sid}/workspace/fork`   | Body: `{target_session_id?, title?}`. Creates a fresh session, copies the source snapshot wholesale, returns the new `session_id`. |

The React SDK exposes two hooks on top of these:

```tsx
const ws = useWorkspaceSnapshot();
ws.downloadSnapshot();          // "Save a copy"
ws.forkSession({ title: "…" }); // "Fork workspace" → new session_id
ws.importFromFile(file);         // "Import from file…"

const { hasPendingWrites, lastSavedAt } = useWorkspacePersistence();
// drive a "Saving…" / "Saved ✓" indicator
```

Covered by behavior tests **WSP01–WSP07** (see
`docs/BEHAVIOR_TEST_REPORT.md`).

### When to use

- Builder agents (digitorn-builder, workflow editors)
- Long-running agents with visible progress (research, code analysis)
- Multi-agent orchestration displays
- Anywhere an n8n-style canvas would help the user follow along

### When NOT to use

- Pure chat apps (digitorn-chat) — no visual state
- Short one-shot apps that run in < 5s — overhead of spawning a Node
  dev server isn't worth it
- Apps where the UI must mutate back to the agent (preview is
  push-only; use `ask_user` for structured user input)

---

## Reference implementation: digitorn-builder

The `digitorn-builder` builtin is the canonical example. It uses ReactFlow
for the canvas, a live compile-status badge, and a state timeline mapped
to the builder's canonical states.

```
packages/digitorn/builtins/digitorn-builder/
├── app.yaml                  (with preview: block)
├── package.toml
├── web/
│   ├── package.json          (react + reactflow + vite + @digitorn/preview-sdk)
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── index.html
│   ├── README.md
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── lib/
│       │   └── yaml-to-graph.ts
│       └── components/
│           ├── CustomNode.tsx
│           ├── DetailPanel.tsx
│           ├── CompileStatus.tsx
│           ├── ConnectionBadge.tsx
│           └── StateTimeline.tsx
```

Copy this structure for any new preview-enabled app. The SDK is installed
as the `@digitorn/preview-sdk` npm package, so nothing needs to be
manually copied.
