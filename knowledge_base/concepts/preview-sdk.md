---
id: preview-sdk
title: "@digitorn/preview-sdk — React SDK for live preview apps"
type: concept
keywords: [preview, sdk, react, hooks, socket.io, websocket, workspace, files, live, realtime, lovable, canvas, slides, npm, package]
related: [preview-module, bundle-namespaces, workspace-module]
source: packages/digitorn-preview-sdk/
---

# @digitorn/preview-sdk — React SDK for live preview apps

## What it is

An npm package that handles **all** the plumbing between a Digitorn
daemon and a React preview app: Socket.IO connection, authentication,
event routing, state management, reconnection. The developer writes
only business logic (how to render files, slides, nodes, etc.).

Install:

```bash
npm install @digitorn/preview-sdk
```

## Quick start — minimal preview app

```tsx
// main.tsx
import { DigiPreview } from "@digitorn/preview-sdk";
import App from "./App";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <DigiPreview>
    <App />
  </DigiPreview>
);

// App.tsx
import { useFiles, useConnection, useAgentStatus } from "@digitorn/preview-sdk";

export default function App() {
  const files = useFiles();          // Map<path, {content, language, size, lines}>
  const connected = useConnection(); // true | false
  const status = useAgentStatus();   // "idle" | "thinking" | "working" | "done" | "error"

  const code = files.get("src/App.tsx")?.content ?? "// waiting...";
  return <pre>{code}</pre>;
}
```

That's it. The SDK reads `session_id` and `token` from the URL
automatically, connects via WebSocket, joins the session room, and
feeds all events into React hooks.

## Provider: `<DigiPreview>`

Wraps the React tree. Owns the Socket.IO connection. Reads session
info from the URL query params (`?session_id=xxx&token=yyy`).

```tsx
<DigiPreview>
  <MyApp />
</DigiPreview>

// Optional: override session info (for testing / Storybook)
<DigiPreview session={{ appId: "test", sessionId: "123", token: null, baseUrl: "http://localhost:8000" }}>
  <MyApp />
</DigiPreview>
```

## All hooks

### Connection

| Hook | Returns | Description |
|------|---------|-------------|
| `useConnection()` | `boolean` | `true` when Socket.IO is connected |

### Workspace files (most common)

| Hook | Returns | Description |
|------|---------|-------------|
| `useFiles()` | `Map<path, WorkspaceFile>` | All files written by the agent |
| `useFile(path)` | `string \| undefined` | Raw content of one file |
| `useFileJson<T>(path)` | `T \| undefined` | Parsed JSON file |
| `useFilesByPrefix(prefix)` | `Array<WorkspaceFile & {path}>` | Files matching a prefix |
| `useFilesJsonByPrefix<T>(prefix)` | `Array<{path, data: T}>` | Parsed JSON files by prefix |

`WorkspaceFile` shape: `{ content: string, language: string, size: number, lines: number }`

### Generic resources (any channel)

| Hook | Returns | Description |
|------|---------|-------------|
| `useResources<T>(channel)` | `Map<string, T>` | All resources in a channel |
| `useResource<T>(channel, id)` | `T \| undefined` | One resource by id |

### Scalar state

| Hook | Returns | Description |
|------|---------|-------------|
| `usePreviewState<T>(key, default?)` | `T \| undefined` | Watch a state key |

### Canvas (nodes / edges)

| Hook | Returns | Description |
|------|---------|-------------|
| `useNodes()` | `PreviewNode[]` | Sorted by updated_at |
| `useEdges()` | `PreviewEdge[]` | All edges |

### Agent feedback

| Hook | Returns | Description |
|------|---------|-------------|
| `useAgentStatus()` | `AgentStatus` | `"idle"` / `"thinking"` / `"working"` / `"done"` / `"error"` |
| `useAgentStream()` | `string` | Accumulated text of current turn |
| `useToolCalls()` | `ToolCall[]` | Last 50 tool calls |
| `useApprovalRequest()` | `ApprovalRequest \| null` | Non-null when agent waits for confirmation |

### Event log

| Hook | Returns | Description |
|------|---------|-------------|
| `useEvents(filter?)` | `PreviewEvent[]` | Last 100 events, optionally filtered |

## Connection protocol (internal — developers don't need to know this)

1. SDK reads `session_id` and `token` from URL query params
2. Connects to `ws://{origin}/events?token={jwt}` via Socket.IO
3. **WebSocket only** — HTTP polling is disabled (causes 400 errors)
4. **No extraHeaders** — browsers don't support custom WebSocket headers
5. On connect, emits `join_session { app_id, session_id, since: lastSeq }`
6. Server sends a `preview:snapshot` with full state on join
7. Subsequent events are incremental deltas

## Events handled by the SDK

### Preview events (from workspace/preview module)

| Event | Reducer action |
|-------|---------------|
| `preview:snapshot` | Full state replace |
| `preview:resource_set` | Upsert resource in channel |
| `preview:resource_patched` | Partial update |
| `preview:resource_deleted` | Remove from channel |
| `preview:resource_bulk_set` | Batch upsert |
| `preview:channel_cleared` | Clear all resources in channel |
| `preview:state_changed` | Update one state key |
| `preview:state_patched` | Merge into state |
| `preview:cleared` | Reset everything |

### Agent events

| Event | Reducer action |
|-------|---------------|
| `token` / `out_token` | Append to agent stream |
| `thinking` / `thinking_started` | Set status to "thinking" |
| `tool_start` | Add to tool calls, status "working" |
| `tool_call` | Attach result to last tool call |
| `turn_complete` / `stream_done` | Reset stream, status "idle" |
| `abort` | Reset stream, status "idle" |
| `approval_request` | Set pending approval |

## Types exported

```typescript
SessionInfo, NodeStatus, WorkspaceFile, PreviewNode, PreviewEdge,
PreviewEvent, PreviewSnapshot, AgentStatus, AgentToken, ToolCall,
ApprovalRequest, DigiPreviewContextValue, ResourceMap
```

## How the agent drives the preview

The agent does NOT call preview module directly. It uses the
**workspace module** (6 actions):

```
WsWrite(path, content)     → preview:resource_set on "files" channel
WsEdit(path, old, new)     → preview:resource_set (updated file)
WsDelete(path)             → preview:resource_deleted
WsRead(path)               → no event (read-only)
WsGlob(pattern)            → no event (read-only)
WsGrep(pattern)            → no event (read-only)
```

The workspace module calls `preview.set_resource("files", path, payload)`
internally. The developer never has to think about the event layer.

## Creating a new preview app from scratch

### Step 1 — app.yaml

```yaml
app:
  app_id: my-app
  name: "My App"

modules:
  workspace:
    config:
      render_mode: react
      entry_file: src/App.tsx
  preview: {}

agents:
  - id: main
    brain:
      provider: anthropic
      model: claude-haiku-4-5-20251001
      config: { api_key: "claude-code" }
    system_prompt: |
      You generate React code. Write files with workspace tools.

execution:
  mode: conversation
  entry_agent: main

capabilities:
  default_policy: block
  grant:
    - module: workspace
      actions: [write, read, edit, glob, grep, delete]

workspace:
  render_mode: react
  entry_file: src/App.tsx

preview:
  enabled: false
```

### Step 2 — preview/ folder

```bash
npm init vite preview -- --template react-ts
cd preview
npm install @digitorn/preview-sdk
```

### Step 3 — preview/src/main.tsx

```tsx
import { DigiPreview } from "@digitorn/preview-sdk";
import App from "./App";
ReactDOM.createRoot(document.getElementById("root")!).render(
  <DigiPreview><App /></DigiPreview>
);
```

### Step 4 — preview/src/App.tsx (your business logic)

```tsx
import { useFiles, useAgentStatus } from "@digitorn/preview-sdk";
export default function App() {
  const files = useFiles();
  const status = useAgentStatus();
  // render however you want
}
```

### Step 5 — build and deploy

```bash
cd preview && npm run build
# dist/ is served automatically by the daemon
```
