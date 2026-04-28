---
id: preview-module
title: "Preview module - universal live canvas for apps"
type: concept
keywords: [preview, canvas, live, reactflow, socket.io, websocket, session, web, iframe, realtime, node, dev_server, hmr, vite, builder, workspace, sdk]
related: [package, agents, bundle-namespaces, builder-state-machine]
source: docs/PREVIEW.md
---

# Preview module - universal live canvas

## What it is

The preview module gives any Digitorn app a **per-session live canvas**
rendered in a Vite/React webapp. The agent writes files and state
through the **workspace module** (WsWrite, WsRead, WsEdit, WsGlob,
WsGrep, WsDelete); the workspace module calls the preview module
internally to fan each mutation out via Socket.IO WebSocket; the web
bundle under `web/` renders them live via the `@digitorn/preview-sdk`
npm package.

Three moving parts snap together:

1. **The `preview` module** - a Python module with 17 internal actions
   (`set_state`, `set_resource`, `emit`, ...) that updates a per-session
   store and publishes deltas. ALL actions are `internal=True` - the
   LLM agent never calls them directly.
2. **The `workspace` module** - 6 agent-visible actions (WsWrite,
   WsRead, WsEdit, WsGlob, WsGrep, WsDelete) that operate on an
   in-memory file tree. Every mutation calls `preview.set_resource()`
   under the hood to stream changes to the client.
3. **The `@digitorn/preview-sdk` npm package** - installs from npm,
   connects via Socket.IO WebSocket (namespace `/events`, `join_session`
   handshake), replays the snapshot on connect, and exposes React hooks.

## Architecture

```
agent tool call         daemon                         browser (inside iframe)
─────────────────       ──────────────────────         ─────────────────────────
WsWrite / WsEdit  ──▶  workspace module
                          │
                          ▼
                        preview.set_resource   ──▶     Socket.IO /events
                        preview.set_state              DigiPreview provider
                        (per session_id)               useFiles(), useNodes()
                        fan-out queue                  useAgentStatus(), ...
```

Two users opening two sessions each get **completely independent
canvas state** - the store is keyed by `session_id`, the Socket.IO
fan-out is per-session, the React SDK joins the session room via
`join_session`.

## Agent-facing tools (workspace module)

The agent uses these 6 tools - same API pattern as the filesystem
module, so the agent does not need to know files live in memory:

| Tool name | Action | Purpose |
|-----------|--------|---------|
| WsWrite   | write  | Create or overwrite a file |
| WsRead    | read   | Read file content (with offset/limit) |
| WsEdit    | edit   | Replace old_string with new_string |
| WsGlob    | glob   | Find files by glob pattern |
| WsGrep    | grep   | Search file contents by regex |
| WsDelete  | delete | Remove a file |

The agent does NOT call `preview.push_node` or `preview.set_state`
directly. The workspace module calls preview internally for every
mutation.

## Preview actions (all internal, called by workspace)

| Action | Purpose |
|---|---|
| `set_state(key, value)` | update one scalar in the session state map |
| `patch_state(patch)` | merge a dict into the state map |
| `get_state()` | return full snapshot |
| `clear()` | wipe state + resources + event buffer |
| `emit(event_type, data)` | push a free-form event (no state change) |
| `set_resource(channel, id, payload)` | upsert one resource in a channel |
| `patch_resource(channel, id, patch)` | partial update of a resource |
| `delete_resource(channel, id)` | remove one resource |
| `bulk_set_resources(channel, items, replace)` | batch upsert resources |
| `clear_channel(channel)` | wipe all resources in a channel |

Every mutation has a monotonic `seq` per session - the React SDK uses
it to deduplicate after a reconnect and to keep the event buffer
ordered.

## Transport: Socket.IO WebSocket

The SDK connects via Socket.IO, NOT SSE. Critical rules:

- **Namespace**: `/events` - the daemon exposes a Socket.IO namespace
  at this path.
- **Transports**: `["websocket"]` only - polling causes 400 errors
  because the auth middleware rejects non-WebSocket upgrade requests.
- **Token**: passed as a URL query parameter, NOT via `extraHeaders`.
  Browsers do not support custom headers on WebSocket connections.
- **Handshake**: after `connect`, the SDK emits `join_session` with
  `{ app_id, session_id, since }`. The daemon responds with a
  `snapshot` event carrying the complete current state, then streams
  `delta` events for subsequent mutations.
- **Reconnect**: exponential backoff (500ms to 10s cap). On reconnect,
  `join_session` sends `since: lastSeq` so the daemon can skip
  already-delivered events.

```typescript
const socket = io(`${baseUrl}/events?token=${token}`, {
  transports: ["websocket"],
  forceNew: true,
  reconnectionDelay: 500,
  reconnectionDelayMax: 10_000,
});
```

## React SDK: @digitorn/preview-sdk

Install from npm:

```bash
npm install @digitorn/preview-sdk
```

### Provider

Wrap your app in `<DigiPreview>` (replaces the old `<PreviewProvider>`):

```tsx
import { DigiPreview } from "@digitorn/preview-sdk";

function main() {
  return (
    <DigiPreview>
      <App />
    </DigiPreview>
  );
}
```

`<DigiPreview>` reads `session_id` and `token` from the URL query
params (or the parent iframe's URL), connects to Socket.IO, and
provides context to all hooks.

Props:
- `children: ReactNode` - required
- `session?: SessionInfo` - override for testing/Storybook
- `maxReconnectMs?: number` - cap for reconnect backoff (default 10s)

### Hooks

| Hook | Return type | Description |
|------|-------------|-------------|
| `useFiles()` | `Map<string, WorkspaceFile>` | All workspace files |
| `useFile(path)` | `string \| undefined` | Raw content of a single file |
| `useFileJson<T>(path)` | `T \| undefined` | Parsed JSON content of a file |
| `useFilesByPrefix(prefix)` | `Array<WorkspaceFile & { path }>` | Files matching a path prefix, sorted |
| `useFilesJsonByPrefix<T>(prefix)` | `Array<{ path, data: T }>` | Parsed JSON files under prefix |
| `useConnection()` | `boolean` | Socket.IO connected status |
| `useAgentStatus()` | `AgentStatus` | `"idle" \| "thinking" \| "working" \| "done" \| "error"` |
| `useAgentStream()` | `string` | Accumulated text of the current agent turn |
| `useToolCalls()` | `ToolCall[]` | Recent tool calls with params and results |
| `useApprovalRequest()` | `ApprovalRequest \| null` | Pending approval request, or null |
| `useNodes()` | `PreviewNode[]` | Canvas nodes (sorted by updated_at) |
| `useEdges()` | `PreviewEdge[]` | Canvas edges |
| `usePreviewState(key)` | `T \| undefined` | Single scalar state value |
| `useResources(channel)` | `Map<string, T>` | All resources in a named channel |
| `useResource(channel, id)` | `T \| undefined` | Single resource by channel and id |
| `useEvents(filter?)` | `PreviewEvent[]` | Raw event log (last 100), optionally filtered |

### Key types

```typescript
interface WorkspaceFile {
  content: string;
  language: string;
  size: number;
  lines: number;
}

type AgentStatus = "idle" | "thinking" | "working" | "done" | "error";

interface ToolCall {
  tool: string;
  params: Record<string, unknown>;
  result?: Record<string, unknown>;
  timestamp: number;
}

interface ApprovalRequest {
  request_id: string;
  tool: string;
  params: Record<string, unknown>;
}

interface SessionInfo {
  appId: string;
  sessionId: string;
  token: string | null;
  baseUrl: string;
}
```

## Wiring an app to use preview + workspace

**Step 1** - declare modules and workspace config:

```yaml
modules:
  preview: {}
  workspace:
    config:
      render_mode: react          # react | builder | latex | slides | html | markdown | code | auto
      entry_file: src/App.tsx     # main file for the client to render first
      title: My App
      sync_to_disk: false         # mirror writes to real filesystem
      lint: true                  # run diagnostics on every write/edit
      instructions: |
        You are building a React app with Tailwind CSS...

preview:
  enabled: true
  command: [npm, run, dev]
  cwd: ./web
  port: 5174
  install_command: [npm, install]
  health_path: /
  startup_timeout: 90
  restart_on_crash: true
```

**Step 2** - ship a `web/` folder next to `app.yaml`:

```
my-app/
├── app.yaml
├── package.toml
└── web/
    ├── package.json        (vite + react + @digitorn/preview-sdk)
    ├── vite.config.ts
    ├── index.html
    └── src/
        ├── main.tsx
        └── App.tsx
```

At deploy time the daemon:

1. Runs `npm install` once (idempotent via marker file)
2. Spawns `npm run dev` with `PORT=5174` in the env
3. Polls the TCP port until the server is ready
4. Serves the iframe at `/api/apps/{id}/preview/`
5. Bridges the HMR WebSocket for Vite hot reload

On undeploy the dev server process is killed gracefully (SIGTERM +
5s grace + SIGKILL).

## Workspace sync_to_disk isolation

When `sync_to_disk: true`, the workspace mirrors files to a real
directory on disk. The sync path is resolved in this priority order:

1. `sync_path` from YAML config (explicit path)
2. `ctx.workspace` (user folder assigned by the session)
3. `~/.digitorn/workspaces/{app_id}/{session_id}/` (fallback)

This ensures each session gets an isolated directory. The agent does
not need to know about the disk path - it writes to virtual paths
like `src/App.tsx`, and the workspace module handles the mapping.

When sync is active:
- `write` / `edit` writes updated content to `{sync_dir}/{path}`
- `delete` removes the file from disk
- `read` does a read-through: if a file is not in memory but exists
  on disk, it loads it first
- `glob` / `grep` scan disk for files not yet loaded, then search all

## Per-session isolation

The module store is `dict[session_id, PreviewSessionState]`. When the
agent loop sets the active session at turn start (via
`preview_module.set_active_session(session_id)`), every subsequent
action mutates **that session's** state only. Two sessions running in
parallel see zero cross-talk.

The Socket.IO namespace uses session rooms - each browser only
receives deltas for its own session after `join_session`.

## Snapshot replay + reconnect

The `<DigiPreview>` provider maintains a single Socket.IO connection.
On disconnect it backs off (500ms to 10s cap) and reconnects. On
`join_session`, the daemon sends a `preview:snapshot` event carrying
the complete current state (state map, resources, events, seq) so the
UI hydrates instantly. Subsequent frames are `preview:*` delta events
ordered by seq.

If the client reconnects mid-session, the snapshot brings it back to
the exact state - no missed updates, no drift.

## Static-bundle preview vs Vite dev server

Two preview modes coexist:

1. **`mode: dev_server`** - `preview.enabled: true`, daemon spawns
   Vite, proxies HTTP + WebSocket. Heavy (~150 MB RAM/app) but
   supports HMR.
2. **`mode: static`** - `preview.enabled: false` AND
   `web/dist/index.html` exists. Daemon serves static files directly.
   Zero processes per app.

## When to use it

- Any app that has a visual representation of its work in progress
  (builder, workflow editor, graph visualizer, data pipeline monitor)
- Any app that benefits from a live canvas (ReactFlow, slides, LaTeX)
- Any long-running conversational agent where the user wants to SEE
  what is happening, not just read a chat log
- Apps generating real code (Lovable-style, React sandboxes, LaTeX)
  with `sync_to_disk: true`

## When NOT to use it

- Purely conversational apps (digitorn-chat) - no canvas makes sense.
- Apps whose UI is highly interactive and needs bidirectional state
  (the agent can push to the UI, but the UI cannot mutate state back
  through the preview module - user actions need to go through
  ask_user or dedicated API routes).
- Short one-shot apps that run for < 5 seconds - the overhead of
  spawning a Node dev server is not worth it.

## YAML reference

```yaml
preview:
  enabled: true                    # false to temporarily disable
  command: [npm, run, dev]         # required; any command
  cwd: ./web                       # working dir, relative to the bundle
  port: 5174                       # the port the dev server binds to
  env:                             # extra env vars for the dev server
    VITE_API_URL: http://localhost:8000
  install_command: [npm, install]  # runs once, marker-guarded
  health_path: /                   # HTTP path for readiness probing
  startup_timeout: 60.0            # seconds to wait for readiness
  restart_on_crash: true           # 3-retry budget per 60s window

workspace:
  render_mode: react               # Flutter client reads this from API
  entry_file: src/App.tsx          # main file to render first
  title: "My App"

modules:
  workspace:
    config:
      render_mode: react
      entry_file: src/App.tsx
      title: My App
      sync_to_disk: false
      sync_path: null              # defaults to session workspace dir
      lint: true
      instructions: |
        Custom instructions prepended to workspace tool prompts...
      tool_instructions:
        write: "Custom write instructions..."
```

## See also

- `@digitorn/preview-sdk` - the npm package (source in
  `packages/digitorn-preview-sdk/src/`)
- `digitorn-builder/web/` - the reference implementation with
  ReactFlow canvas, live YAML panel, and state timeline.
- `docs/PREVIEW.md` - long-form guide with the full lifecycle diagram.
