# @digitorn/preview-sdk

React SDK for building custom UIs inside Digitorn app preview iframes.
**Your iframe shares the same agent runtime as the workbench chat** — same memory, same tools, same approvals. The chat panel is no longer the only surface; this SDK lets you build any agentic UX (chat clone, code editor, settings panel, voice interface, dashboard, formulaire) on top of the same daemon.

```bash
npm install @digitorn/preview-sdk react react-dom socket.io-client
```

```tsx
import { DigiPreview, useChat } from "@digitorn/preview-sdk";

function App() {
  const chat = useChat();
  return (
    <div>
      {chat.messages.map((m, i) => <p key={i}>{m.role}: {m.content}</p>)}
      <button onClick={() => chat.send("hello")}>Send</button>
      {chat.busy && <button onClick={() => chat.abort()}>Stop</button>}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <DigiPreview><App /></DigiPreview>,
);
```

## Hooks

### Conversational driver

- **`useChat()`** — send / abort / retry + live transcript over Socket.IO. The all-in-one for chat-centric apps.
- **`useStream()`** — typed content blocks of the **current** turn (thinking, text, tool_use, citation). Build Claude.ai-style UIs with collapsible reasoning + inline tool widgets.
- **`useApprovals()`** — pending list + `approve` / `reject` imperative for capability-gated tool calls.

### Files & workspace

- **`useFiles()` / `useFile(path)` / `useFileJson(path)`** — live file tree + content from the workspace channel.
- **`useWorkspaceFiles()`** — `readFile` / `writeFile` / `uploadFile` / `deleteFile` / `approveFile` / `rejectFile` / `commit`. Mirrors the agent-side Ws* tools.
- **`useWorkspaceSnapshot()`** — export / import / fork the entire session workspace.
- **`useFilesByPrefix(prefix)` / `useFilesJsonByPrefix(prefix)`** — typed convenience filters.
- **`useFileStats()`** — counts + dirty / approved totals.

### Code editor (VS Code-like)

- **`useCodeState()`** — file tree + meta.
- **`useFileContent(path)` / `useFileActions()`** — open / save / revert.
- **`useDiagnostics()` / `useDiagnosticsStats()`** — LSP errors / warnings.
- **`useLspRequest()`** — generic LSP RPC bridge.
- **`useCodeStats()`** — totals for status bar.

### Session lifecycle

- **`useSessionMeta()`** — session_id, app_id, created_at, turn_count, is_first_visit, workspace, workdir.
- **`useSessionLifecycle({ onFirstVisit, onResume, onReady })`** — one-shot lifecycle dispatcher with idempotent guards.

### Live state

- **`useConnection()`** — Socket.IO connection status.
- **`useResources(channel)` / `useResource(channel, id)`** — generic resource maps.
- **`usePreviewState(key, defaultValue?)`** — scalar state values.
- **`useNodes()` / `useEdges()`** — graph helpers.

### Agent observability (raw)

- **`useAgentStatus()`** — `idle | thinking | tool_use | streaming | error`.
- **`useAgentStream()`** — accumulated text of current turn (use `useStream` for typed blocks).
- **`useToolCalls()`** — flat list of recent tool calls.
- **`useApprovalRequest()`** — *(deprecated)* head of the approvals queue.
- **`useEvents(filter?)`** — raw event log.

### Host integration (iframe ↔ workbench)

- **`useHostTheme()`** — dark / light / accent / locale resolved from URL or postMessage.
- **`useHostMessage(type, handler)`** — listen for theme-change, locale-change, abort, resize.
- **`sendToHost({ type, ... })`** / **`requestOpenFile`** / **`requestFocusLine`** / **`requestToast`** / **`notifyReady`** — outbound iframe → host events.

## Wire architecture

| What | Channel | Detail |
|---|---|---|
| Live state (files, resources, agent status, tokens) | Socket.IO `event` | reduced into a typed React state tree |
| HTTP one-shot snapshot on mount | `GET /sessions/{sid}/preview` | so reopen rehydrates the canvas |
| Send message / abort / approve / reject | Socket.IO emit + ack | `send_message`, `abort_turn`, `resolve_approval` |
| File mutations (write, upload, delete, commit) | HTTP REST | `PUT/POST/DELETE /workspace/files/...` |
| Snapshot export / import / fork | HTTP REST | `POST /workspace/snapshot/...` |

**Key principle**: actions over the persistent WebSocket (no second handshake), file mutations over REST (right protocol for blobs).

## Hidden namespaces

Three glob patterns are auto-routed to the daemon-private workspace, never the user's workdir:

| Glob | Use case |
|---|---|
| `__sdk__/**` | SDK-private files (preferences, welcome flags, layout state) |
| `.app/**` | Application-private state (per-session bookmarks, drafts) |
| `.digitorn/**` | Daemon metadata (baselines, session history, transient cache) |

```tsx
const fs = useWorkspaceFiles();
await fs.writeFile("__sdk__/prefs.json", JSON.stringify(prefs));
// Lands in ~/.digitorn/workspaces/{app}/{sid}/__sdk__/prefs.json
// NEVER in the user's project, even with sync_to_disk: true
```

## Documentation

Full reference — hook signatures, REST contract, host protocol, wire path tables, end-to-end examples:

→ **[docs/app-language/47-preview-sdk.md](../../docs/app-language/47-preview-sdk.md)**

Related:

- [Workspace & Preview](../../docs/app-language/41-preview.md)
- [Client Manifest](../../docs/app-language/44-client-manifest.md)
- [API Integration](../../docs/app-language/14-api-integration.md)

## Building this package

```bash
cd packages/digitorn-preview-sdk
npm run build         # compiles TypeScript to dist/
npm run dev           # tsc --watch
```

Peer-deps: React ≥ 18, socket.io-client ≥ 4.7. The `dist/` folder is the published output.
