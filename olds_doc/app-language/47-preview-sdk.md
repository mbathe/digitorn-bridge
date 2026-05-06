---
id: preview-sdk
---

# `@digitorn/preview-sdk` — Building Frontends for Digitorn Apps

The preview SDK is a React package that lets you ship a custom UI inside a
Digitorn app's preview iframe. It wires the iframe to the daemon's
Socket.IO event bus, hydrates the initial state via HTTP, and exposes
hooks that mirror the agent's view of the world: files, resources, agent
status, diagnostics, lifecycle.

If your app produces visible artefacts (a code editor, a Lovable-style
sandbox, a settings panel, a custom dashboard), this is the surface you
build against.

| | |
|---|---|
| **Package** | `@digitorn/preview-sdk` |
| **Source** | `packages/digitorn-preview-sdk/src/` |
| **Peers** | `react ≥ 18`, `socket.io-client ≥ 4.7` |
| **Entry** | `<DigiPreview>` provider — wraps the app, opens the WebSocket, fetches the snapshot |

The SDK is also the only sanctioned way to write into the
**daemon-private workspace** (state.json, `__sdk__/` hidden namespaces,
baselines) without leaking those files to the user's project tree. See
[Hidden namespaces](#hidden-namespaces--workspace-vs-workdir) below.

---

## What you can build today

The SDK now covers a wide surface. The iframe is no longer just a
read-only preview — it is a peer of the workbench chat panel, sharing
the same agent runtime. Here is what is buildable end-to-end with the
hooks that ship today:

### Chat-centric apps

- **Custom Claude.ai-clone or ChatGPT-clone** — `useChat` for send /
  abort / retry, `useStream` for typed thinking + text + tool_use +
  citation blocks, `useChat().messages` for the persisted transcript.
  Render collapsible thinking, inline tool widgets, clickable
  citations, exactly like Claude.ai.
- **Conversational form / wizard** — call `useChat().send()` from form
  submits, render replies with `useStream().blocks`. The agent can
  validate fields, suggest corrections, run server-side actions.
- **Voice-first / hands-free** — wire a speech-to-text widget to
  `useChat().send()`, read back via TTS while watching `useStream()`
  for live progress.

### Code-centric apps

- **Lovable / v0-style sandbox** — `useFiles` for the live tree,
  `useFileContent` + Monaco for editing, `PreviewProxy` (agent-side)
  for the running dev server. The agent writes files, the user sees
  them update live.
- **Cursor-like pair-programming** — `useChat` for the conversation,
  `useApprovals` to gate every risky tool call (write, delete,
  shell), `useFileActions` for apply / revert per-hunk.
- **VS Code-like editor** — `useCodeState` + `useDiagnostics` +
  `useLspRequest` give a full LSP loop with errors, hover, completion.

### Compliance / safety apps

- **Approval-gated agent for finance / health / legal** —
  `useApprovals().pending` + `approve` / `reject` blocks every risky
  call. Combine with `useToolCalls` for the audit trail.
- **Gradual-rollout tools** — show live what the agent intends to do,
  let the user opt in per action.

### Lifecycle-aware apps

- **First-visit wizard / onboarding** — `useSessionLifecycle({
  onFirstVisit })` fires once on a fresh session, persists the
  "shown" flag in `__sdk__/welcome.json`.
- **Resume-aware UI** — `onResume` fires on returning users with
  `meta.idleSeconds`, drive a "welcome back, here is what you missed"
  panel.
- **Settings panels** — `useWorkspaceFiles` writes prefs to
  `__sdk__/settings.json` (auto-routed to the daemon-private dir,
  never leaks to the user's project).

### File-driven workflows

- **Document analysis app** — drag-drop a PDF, the agent processes it
  via tools, the iframe shows progress in `useStream`, citations land
  as inline blocks.
- **Time-travel / fork** — `useWorkspaceSnapshot` exports a session as
  a portable envelope, imports / forks for "what-if" exploration.
- **Drag-drop import / export** — `useWorkspaceFiles().uploadFile`
  takes a Blob, the daemon stores it 1:1.

### Observability / dashboards

- **Tool inspector** — `useToolCalls` lists every tool call with its
  status; `useStream().toolUses` intercalates them with the text
  output.
- **Live agent status board** — `useAgentStatus` + `useEvents` for a
  raw timeline UI suitable for debugging or replaying a session.
- **Custom analytics** — `useResources(channel)` reads any namespaced
  channel the agent writes to (`metrics`, `logs`, `events`...).

### Multi-surface coexistence

The workbench chat keeps working unchanged. Your iframe app coexists
with it:

- The agent sees one stream of user messages from BOTH surfaces.
- Both UIs see the same `useChat().messages` history.
- Approvals raised by the agent appear in BOTH surfaces' approval
  modals; resolving in one closes both.

This means an app can ship its own UX and let the user fall back to
the workbench chat when needed — the runtime is shared.

### What still needs more SDK work (next phases)

| Gap | Phase | Unlocks |
|---|---|---|
| `useAgents()` for sub-agent visibility | Phase 2 | Devin-clone, multi-agent dashboards |
| `useMemory` / `useGoal` / `useTodos` | Phase 2 | "What is the agent thinking" panels, project trackers |
| `useFileHistory(path)` | Phase 2 | Time-travel UX, audit timeline |
| Voice / audio bridge | Phase 3 | Voice assistants, accessibility |
| Watch user → agent reactivity | Phase 3 | Live pair programming, Cursor-Composer-like |
| Daemon watchdog + supervisor | Phase 1.4 | Production-grade uptime + auto-restart |
| Testing utilities + CLI scaffold | Phase 3 | DX, faster adoption |

---

## Quick start

A new SDK app is a tiny Vite + React project served from the daemon under
`/api/apps/{app_id}/web-static/`. The daemon attaches the iframe
automatically when the app's bundle ships a `web/dist/index.html`.

### 1. Install

```bash
npm install @digitorn/preview-sdk react react-dom socket.io-client
```

### 2. Mount `<DigiPreview>` at the root

```tsx
// src/main.tsx
import React from "react";
import { createRoot } from "react-dom/client";
import { DigiPreview } from "@digitorn/preview-sdk";
import { App } from "./App";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <DigiPreview>
      <App />
    </DigiPreview>
  </React.StrictMode>,
);
```

### 3. Use hooks anywhere in the tree

```tsx
import {
  useFiles, useAgentStatus, useToolCalls, useWorkspaceFiles,
} from "@digitorn/preview-sdk";

export function App() {
  const files = useFiles();              // live workspace tree
  const status = useAgentStatus();       // 'idle' | 'thinking' | 'tool_use' | ...
  const tools = useToolCalls();          // recent tool calls
  const fs = useWorkspaceFiles();        // imperative file ops

  return (
    <div>
      <header>Agent: {status}</header>
      <ul>{[...files.keys()].map(p => <li key={p}>{p}</li>)}</ul>
      <button onClick={() => fs.writeFile("notes.md", "# Hello")}>
        Save notes.md
      </button>
    </div>
  );
}
```

### 4. Build + serve

```bash
npx vite build    # outputs to web/dist/
```

Vite's `base` must be set to `'./'` so the compiled bundle resolves
assets relative to the iframe URL:

```ts
// vite.config.ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "web/dist",
    emptyOutDir: true,
  },
});
```

Drop `web/dist/` next to the app's `app.yaml` and redeploy:

```
my-app/
├── app.yaml
└── web/
    └── dist/
        ├── index.html
        ├── assets/index-XXXX.js
        └── assets/index-XXXX.css
```

```bash
digitorn app deploy my-app/app.yaml
```

The daemon serves `web/dist/index.html` at
`/api/apps/{app_id}/web-static/index.html` and the workbench iframe
auto-points there. No Node child process — pure static.

---

## How it fits the rest of Digitorn

```
┌─────────────────────┐               ┌─────────────────────────┐
│ Daemon (FastAPI +   │  Socket.IO    │ Browser iframe          │
│ Socket.IO)          │◀────────────▶ │  ┌────────────────────┐ │
│                     │ HTTP snapshot │  │ <DigiPreview>      │ │
│ ▸ workspace module  │◀──────────── ─│  │   ┌────────────┐   │ │
│ ▸ preview module    │ HTTP mutate   │  │   │ your React │   │ │
│ ▸ filesystem ...    │ ────────────▶ │  │   │   tree     │   │ │
│ ▸ agent loop        │  postMessage  │  │   └────────────┘   │ │
└─────────────────────┘ ◀───────────▶ │  └────────────────────┘ │
                                      └─────────────────────────┘
                                              (iframed in the
                                               Digitorn workbench)
```

- **Socket.IO** carries live deltas (`preview:resource_set`,
  `preview:state_changed`, `agent:token`, `tool_call:*`, ...). The SDK
  reduces them into a single immutable state tree consumed by hooks.
- **HTTP** (`GET /api/apps/{app}/sessions/{sid}/preview`) is fetched
  once on mount to hydrate the canvas before any new agent action lands.
  Without this, the iframe is empty until the next agent write.
- **postMessage** (`<iframe>` ↔ host) carries cross-cutting UX events:
  the host pushes theme/locale changes; the iframe asks the host to open
  a file in its own editor, focus a line, show a toast, navigate.
- **REST** (`PUT|GET|DELETE /workspace/files/{path}`,
  `POST /workspace/files/approve|reject|commit`,
  `POST /workspace/upload/{path}`) is what
  `useWorkspaceFiles` calls under the hood when the iframe writes.

---

## `<DigiPreview>` — the root provider

Defined in `packages/digitorn-preview-sdk/src/DigiPreview.tsx`.

```tsx
<DigiPreview maxReconnectMs={10_000} session={overrideSession}>
  {children}
</DigiPreview>
```

| Prop | Type | Default | Purpose |
|------|------|---------|---------|
| `children` | `ReactNode` | (required) | Your app tree. |
| `session` | `SessionInfo \| undefined` | resolved from URL | Override the auto-detected session info — useful for Storybook / local dev. |
| `maxReconnectMs` | `number` | `10_000` | Cap on the WebSocket reconnect backoff. |

### What it does on mount

1. Resolves `SessionInfo` from the iframe URL (`?session_id=...&token=...`)
   or the parent window's URL when nested.
2. Opens a Socket.IO connection to `{baseUrl}/socket.io` with the bearer
   token; reconnects with exponential backoff up to `maxReconnectMs`.
3. Issues a one-shot HTTP `GET /sessions/{sid}/preview` to hydrate the
   canvas. The reducer merges the snapshot's `state`, `resources` and
   `seq`. Failures are silent — Socket deltas re-hydrate as the agent
   acts.
4. Provides `DigiPreviewContextValue` to descendants. Hooks read from
   that context; they will throw if used outside `<DigiPreview>`.

### `readSession()`

Exported helper that returns the same `SessionInfo` the provider
computes:

```ts
import { readSession } from "@digitorn/preview-sdk";

const { appId, sessionId, token, baseUrl } = readSession();
```

Falls back to `sessionId === "_dev_"` and logs a console warning when
opened directly without `?session_id=...`.

---

## Hooks reference

All hooks must be used inside a `<DigiPreview>` subtree. They subscribe
to the immutable state tree and re-render only when their slice changes.

### Connection

```ts
useConnection(): boolean
```

`true` while the WebSocket is connected. Use it to dim the UI / display
a "reconnecting…" banner during transient drops.

### Generic resources & state

```ts
useResources<T>(channel: string): Map<string, T>
useResource<T>(channel: string, id: string): T | undefined
usePreviewState<T>(key: string, defaultValue?: T): T | undefined
```

`useResources` returns the live map for any channel emitted via
`preview.set_resource` / `bulk_set_resources`. `usePreviewState`
returns scalar values stored via `preview.set_state` / `patch_state`.

### Files (workspace channel)

The workspace module emits files on the `files` channel — these
helpers are typed convenience wrappers.

```ts
useFiles(): Map<string, WorkspaceFile>           // path → file
useFile(path: string): string | undefined         // content of one file
useFileJson<T>(path: string): T | undefined       // auto-parsed JSON
useFilesByPrefix(prefix: string): Array<WorkspaceFile & { path: string }>
useFilesJsonByPrefix<T>(prefix: string): Array<{ path: string; data: T }>
useFileStats(): FileStats     // { count, totalLines, dirty, approved, ... }
```

`WorkspaceFile` carries `content`, `size`, `lines`, `validation`
(`pending|approved|rejected`), `git_status`, `unified_diff_pending`, plus
the `*_pending` / `total_*` insertion/deletion counters used by VS
Code-style diff gutters.

### Imperative file ops — `useWorkspaceFiles`

```ts
const fs = useWorkspaceFiles();

// Read a single file
const { content, size, lines } = await fs.readFile("src/App.tsx");

// Write or overwrite a text file (defaults to validation: pending)
await fs.writeFile("notes.md", "# Hello", { autoApprove: true });

// Upload a binary blob 1:1 (image, archive, audio)
await fs.uploadFile(`avatars/${file.name}`, file, { autoApprove: true });

// Delete + approve / reject lifecycle
await fs.deleteFile("scratch.txt");
await fs.approveFile("README.md");      // snapshot baseline
await fs.rejectFile("README.md");       // revert to baseline (or delete)

// Commit approved files via git
await fs.commit("docs: add README", { push: false });

// While any op is in flight
fs.busy;        // boolean
fs.error;       // Error | null
```

These call REST endpoints under
`/api/apps/{app}/sessions/{sid}/workspace/files/...` and
`/workspace/upload/...`. They mirror the agent-side `Ws*` tools, so an
iframe can manipulate the workspace autonomously — user uploads,
manual edits, drag-drop import, settings persistence.

### Workspace snapshot — export / import / fork

```ts
const ws = useWorkspaceSnapshot();

const envelope = await ws.exportSnapshot();              // portable JSON
await ws.importSnapshot(envelope, { replace: true });    // overwrite session
const fork = await ws.forkSnapshot();                    // clone session id
```

`useWorkspacePersistence` wraps this with auto-save on `beforeunload` for
quick experiments.

### Session metadata — `useSessionMeta`

```ts
interface SessionMeta {
  sessionId: string;
  appId: string;
  createdAt: number;          // epoch seconds, 0 if unknown
  lastActiveAt: number;       // epoch seconds, 0 if unknown
  turnCount: number;          // user + assistant turns persisted
  isFirstVisit: boolean;      // empty state AND no turns yet
  title: string;
  idleSeconds: number;        // convenience, Infinity when unknown
  ageSeconds: number;         // convenience, Infinity when unknown
  workspace: string;          // see [Hidden namespaces] below
  workdir: string;
}

const meta = useSessionMeta();
```

The hook fetches `/sessions/{sid}/preview` on mount and pulls the
`session` block from the snapshot. Defaults to a safe state
(`isFirstVisit: true`, infinite age) when the daemon is unreachable —
showing a wizard once is a smaller UX issue than skipping it forever.

### Session lifecycle — `useSessionLifecycle`

```ts
useSessionLifecycle({
  onFirstVisit: async () => {
    // Fired ONCE the first time the iframe loads with no prior state.
    await fs.writeFile("__sdk__/welcome-shown", "1", { autoApprove: true });
  },
  onResume: meta => {
    // Fired ONCE on a session that has prior state.
    console.log(`Welcome back, last seen ${meta.idleSeconds}s ago`);
  },
  onReady: meta => {
    // Fired on every successful mount, after first-visit / resume.
  },
});
```

The dispatcher is idempotent across remounts during the same session
(module-level `Set<sessionId>` guards each handler). It waits until the
snapshot has actually arrived (`createdAt > 0` or `turnCount > 0`)
before firing, so you don't double-trigger `onFirstVisit` on transient
empty states.

### Code editor hooks — `useCodeState` family

```ts
useCodeState(): CodeStateEntry[]              // ordered files + meta
useFileContent(path: string): FileContentState
useFileActions(): UseFileActionsApi           // open/save/revert imperative
useCodeStats(): CodeStats                     // totals for status bar
useDiagnostics(path: string): DiagnosticsEntry | undefined
useDiagnostics(): Map<string, DiagnosticsEntry>
useDiagnosticsStats(): DiagnosticsStats       // counts by severity
useLspRequest(): UseLspRequestApi             // send LSP RPC, cancel
```

Drop-in replacements when you're building a VS Code-like surface inside
the iframe. The `useDiagnostics` overload returns either one file's
diagnostics or the whole map.

### Agent / runtime hooks

```ts
useAgentStatus(): AgentStatus                 // 'idle' | 'thinking' | 'tool_use' | 'streaming' | 'error'
useAgentStream(): string                      // accumulating assistant tokens
useToolCalls(): ToolCall[]                    // recent tool calls (bounded queue)
useApprovalRequest(): ApprovalRequest | null  // (deprecated) head of the pending queue
useEvents(filter?): PreviewEvent[]            // raw event log (debug / replay UIs)
```

### Structured streaming — `useStream`

The agent's response stream isn't a single text channel. It interleaves
several distinct types of output: chain-of-thought reasoning, the final
answer text, tool calls and their results, and (on supported models)
inline citations to sources. `useStream` exposes them as an ordered,
chronological list of typed `ContentBlock`s so chat UIs render each
kind differently instead of flattening everything to a string.

```ts
type ContentBlock = ThinkingBlock | TextBlock | ToolUseBlock | CitationBlock;

interface ThinkingBlock {
  type: "thinking";
  content: string;            // accumulating chain-of-thought
  streaming?: boolean;        // true while deltas are still arriving
  tokens?: number;
  timestamp: number;
}

interface TextBlock {
  type: "text";
  content: string;            // accumulating final answer
  streaming?: boolean;
  timestamp: number;
}

interface ToolUseBlock {
  type: "tool_use";
  tool: string;               // FQN of the tool
  params: Record<string, unknown>;
  result?: unknown;           // populated on tool_call event
  status: "running" | "done" | "error";
  timestamp: number;
}

interface CitationBlock {
  type: "citation";
  source: string;             // URL or doc id
  quote?: string;
  timestamp: number;
}
```

The hook returns the blocks of the **current** turn (live, growing as
events arrive). Frozen history is on `useChat().messages[i].blocks`
for past assistant messages.

```ts
const { blocks, streaming, thinking, text, toolUses, citations,
        textContent } = useStream();
```

| Field | What it is |
|---|---|
| `blocks` | Full ordered list of ContentBlocks for the current turn |
| `streaming` | True while at least one block is still receiving deltas |
| `thinking` / `text` / `toolUses` / `citations` | Convenience filters by type |
| `textContent` | Concatenated text content, equivalent to `useAgentStream()` |

### A Claude.ai-style structured assistant

```tsx
import { useStream } from "@digitorn/preview-sdk";

function StructuredAssistant() {
  const { blocks, streaming } = useStream();
  if (!streaming && blocks.length === 0) return null;

  return (
    <div className="assistant">
      {blocks.map((b, i) => {
        if (b.type === "thinking") {
          return (
            <details key={i} open={b.streaming} className="thinking">
              <summary>
                Reasoning {b.tokens ? `(${b.tokens} tokens)` : ""}
                {b.streaming && " ..."}
              </summary>
              <pre>{b.content}</pre>
            </details>
          );
        }
        if (b.type === "text") {
          return (
            <p key={i} className="text">
              {b.content}
              {b.streaming && <span className="cursor">▍</span>}
            </p>
          );
        }
        if (b.type === "tool_use") {
          return (
            <div key={i} className={`tool-use status-${b.status}`}>
              <strong>🔧 {b.tool}</strong>
              <pre>{JSON.stringify(b.params, null, 2)}</pre>
              {b.status === "done" && (
                <pre className="result">{JSON.stringify(b.result, null, 2)}</pre>
              )}
              {b.status === "running" && <span>Running...</span>}
              {b.status === "error" && <span>Failed</span>}
            </div>
          );
        }
        if (b.type === "citation") {
          return (
            <a key={i} className="citation" href={b.source} target="_blank">
              [{b.source}]
            </a>
          );
        }
        return null;
      })}
    </div>
  );
}
```

### Wire path — daemon events that drive the blocks

The reducer feeds each block from a specific daemon event:

| Daemon event | Reducer action | Effect on `blocks` |
|---|---|---|
| `thinking_started` | open a fresh `ThinkingBlock` (`streaming: true`) | append at tail |
| `thinking_delta` | append `delta` to the streaming `ThinkingBlock` | mutate tail |
| `token` / `out_token` | append `content` to a streaming `TextBlock` (creates one if needed) | mutate tail |
| `tool_start` | append a `ToolUseBlock` (`status: "running"`) | append at tail |
| `tool_call` | resolve the latest matching `ToolUseBlock` (set result + `status: "done"`) | mutate matching |
| `turn_complete` / `stream_done` | flip every streaming flag to false | freeze in place |
| `abort` | drop the streaming tail (UI never sees a partial reply) | remove tail |

The chronological order of events is preserved. If the agent thinks,
then writes, then calls a tool, then writes more, the blocks land in
that exact sequence. UIs just `.map()` over `blocks` to render the
turn faithfully.

### Convenience filters

When you need just one type:

```tsx
function ToolWidget() {
  const { toolUses } = useStream();
  return (
    <ul>
      {toolUses.map((t, i) => (
        <li key={i}>{t.tool} {t.status}</li>
      ))}
    </ul>
  );
}

function ThinkingPanel() {
  const { thinking } = useStream();
  if (thinking.length === 0) return null;
  return <pre>{thinking.map(t => t.content).join("\n---\n")}</pre>;
}
```

### `useStream` vs `useChat().messages` vs `useAgentStream`

| Hook | When to use |
|---|---|
| `useStream` | Render the **current turn** with rich typed blocks (live) |
| `useChat().messages[i].blocks` | Render **past turns** the same way (frozen) |
| `useAgentStream` | Just need the plain text of the current turn (no structure) |
| `useToolCalls` | Just need a flat list of tool calls (no chronology with text) |

For a Claude.ai-clone, the pattern is: map over `useChat().messages`,
and for the streaming tail (the one with `streaming: true`) read
`useStream().blocks` instead of `message.blocks` (they're the same
reference; `useStream` is just a convenience).

### Conversational driver — `useChat`

The chat panel is no longer the only seat at the table. With `useChat`,
your iframe sends messages, cancels running turns, retries, and reads
the live transcript over the **same** Socket.IO connection that streams
the agent's tokens. The chat surface becomes a peer of any custom UI
you build, sharing the same agent, memory, tools, and approvals.

```ts
const {
  messages, send, abort, retry,
  busy, status, stream, error,
} = useChat();

interface ChatUserMessage {
  role: "user";
  content: string;
  images?: string[];
  timestamp: number;
  correlation_id?: string;
  pending?: boolean;            // settles when turn_complete lands
}

interface ChatAssistantMessage {
  role: "assistant";
  content: string;
  timestamp: number;
  correlation_id?: string;
  tool_calls?: ToolCall[];      // intercalated with the tokens
  streaming?: boolean;          // true while tokens are still arriving
}

interface ChatToolMessage {
  role: "tool";
  tool_call_id: string;
  tool: string;                 // short label
  content: string;              // tool result, stringified
  timestamp: number;
}

type ChatMessage = ChatUserMessage | ChatAssistantMessage | ChatToolMessage;
```

**All wire actions go over Socket.IO** — send, abort, retry. No HTTP,
no second handshake. Mapping to daemon handlers:

| Hook action | Socket.IO event | Daemon handler |
|---|---|---|
| `send(text, opts?)` | `send_message` | enqueue + drain + run turn |
| `abort({purgeQueue?})` | `abort_turn` | cancel task + cancel approvals + optional clear queue |
| `retry()` | replays last user message | (calls `send` again) |

The reducer keeps `messages` in sync from four event types:

| Event | Effect |
|---|---|
| `user_message` | Append a `ChatUserMessage` (de-dup by correlation_id) |
| `token` / `out_token` | Update the streaming `ChatAssistantMessage` tail in place |
| `tool_start` / `tool_call` | Attach the `ToolCall` to the streaming tail's `tool_calls` |
| `turn_complete` / `stream_done` | Flip the streaming flag → finalised message |
| `abort` | Drop the streaming tail (the agent never finished its thought) |

### A complete chat panel in 30 lines

```tsx
import { DigiPreview, useChat } from "@digitorn/preview-sdk";

function ChatPanel() {
  const chat = useChat();
  const [draft, setDraft] = useState("");

  return (
    <div className="chat">
      <ul>
        {chat.messages.map((m, i) => (
          <li key={i} data-role={m.role}>
            {m.role === "user" && <>🙋 {m.content}</>}
            {m.role === "assistant" && (
              <>🤖 {m.content}{m.streaming && <span className="cursor">▍</span>}</>
            )}
            {m.role === "tool" && <code>🔧 {m.tool} → {m.content}</code>}
          </li>
        ))}
      </ul>
      <form onSubmit={async e => {
        e.preventDefault();
        const text = draft.trim();
        if (!text || chat.busy) return;
        setDraft("");
        await chat.send(text);
      }}>
        <input value={draft} onChange={e => setDraft(e.target.value)}
               disabled={chat.busy} />
        <button type="submit" disabled={chat.busy}>Send</button>
        {chat.busy && (
          <button type="button" onClick={() => chat.abort()}>Stop</button>
        )}
      </form>
      <small>Status: {chat.status}</small>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <DigiPreview><ChatPanel /></DigiPreview>,
);
```

### `send` options

```ts
chat.send("hello", {
  images: [{ data: base64, mime: "image/png", name: "shot.png" }],
  queue_mode: "async",          // "wait" blocks the ack until the turn starts
  client_message_id: "abc123",  // idempotency key for retries
});
```

The ack carries the daemon's `correlation_id` plus the queue position
(0 = running now). The optimistic user entry lands in `messages`
instantly; the daemon's echo is de-duped via the same correlation id.

### `abort` semantics

By default, `abort()` cancels just the running turn — queued messages
keep draining. `abort({purgeQueue: true})` drops everything. The hook
also unwinds any approval the agent was awaiting (`approval_queue.cancel_all`),
so an approve-blocked turn doesn't outlive the abort.

### `retry` is just a replay

Looks up the most recent `ChatUserMessage` in `messages`, calls `send`
with its content. No-op when no user message exists yet.

### When to use `useChat` vs the other hooks

| Need | Hook |
|---|---|
| Render a chat transcript | `useChat().messages` |
| Show a "Send" button + Stop while running | `useChat().busy` + `abort` |
| Just stream raw tokens (no transcript) | `useAgentStream()` |
| Just show "thinking…" indicator | `useAgentStatus()` |
| Inspect every tool call | `useToolCalls()` |

`useChat` is the all-in-one for chat-centric apps. Mix and match the
others when you need to render a non-chat UI that still observes the
agent.

### Approval gates — `useApprovals`

Pairs with `tools.capabilities` `policy: approve`. When the agent (or
direct REST tool exec) hits a tool gated for approval, the daemon
queues the call and pushes an `approval_request` Socket.IO event. The
hook exposes the live pending list plus imperative `approve` / `reject`
that resume the suspended tool.

```ts
const { pending, approve, reject, busy, error } = useApprovals();

interface ApprovalRequest {
  request_id: string;
  tool_name: string;                       // e.g. "filesystem.write"
  tool_params: Record<string, unknown>;
  risk_level: string;                       // "low" | "medium" | "high" | "critical"
  description: string;
  agent_id: string;
  user_id: string;
  app_id: string;
  session_id: string;
  created_at: number;                       // epoch seconds
  // Legacy aliases (kept for backward compat):
  tool?: string;                            // = tool_name
  params?: Record<string, unknown>;         // = tool_params
}
```

**Imperative actions travel over Socket.IO**, on the same connection
that delivered the pending request. No HTTP round-trip, no second JWT
verification, no extra TLS handshake. The wire path is the
`resolve_approval` Socket.IO action; the daemon resolves the awaiting
tool's future server-side and emits an `approval_resolved` event so
every subscribed iframe drops the modal in sync.

```tsx
function ApprovalsModal() {
  const { pending, approve, reject, busy } = useApprovals();
  if (pending.length === 0) return null;
  const req = pending[0];

  return (
    <div role="dialog">
      <h2>{req.risk_level.toUpperCase()} action</h2>
      <p><strong>{req.tool_name}</strong> {req.description}</p>
      <pre>{JSON.stringify(req.tool_params, null, 2)}</pre>
      <button disabled={busy} onClick={() => approve(req.request_id)}>
        Approve
      </button>
      <button disabled={busy} onClick={() => reject(req.request_id, "looks risky")}>
        Reject
      </button>
    </div>
  );
}
```

Multiple pending requests are surfaced as a list — the previous
`useApprovalRequest()` (single-modal) hook is now an alias of
`pending[0]`. Build a queued UI by mapping over `pending`:

```tsx
{pending.map(req => (
  <ApprovalCard key={req.request_id} req={req}
                onApprove={() => approve(req.request_id)}
                onReject={() => reject(req.request_id)} />
))}
```

`message` is the optional payload the daemon forwards to the awaiting
tool call. For `ask_user`-style requests it carries the user's actual
answer (option id, free text, JSON form). For plain tool approvals it
carries an optional reason. ALWAYS propagated regardless of the
approve / reject outcome — the awaiting tool decides what it means.

The hook also catches up automatically across reconnects: when the
iframe's WebSocket reconnects, the daemon replays missed events from
the session bus (including `approval_request` events that fired before
the reconnect), so a pending request that landed during a connection
drop still surfaces once the socket re-attaches.

For non-iframe clients (CLIs, mobile, server-to-server), the same
resolution is exposed at `POST /api/apps/{app_id}/approve` with body
`{request_id, approved, message?}` — see [API Integration](14-api-integration.md).

### Graph hooks (for flow-style apps)

```ts
useNodes(): PreviewNode[]
useEdges(): PreviewEdge[]
```

Read from the `nodes` and `edges` resource channels — automatic when the
agent uses `preview.set_resource("nodes", id, payload)`.

### Host integration hooks

```ts
useHostTheme(): HostTheme                     // { mode, accent, locale }
useHostMessage(type, handler): void           // listen to host pushes
```

See [Host ↔ iframe protocol](#host--iframe-protocol) below.

---

## Hidden namespaces — workspace vs workdir

Digitorn cleanly separates **what the user sees** from **what the
daemon needs internally**:

| | Path | Holds |
|---|---|---|
| **`workdir`** | user-supplied at session create, or `~/.digitorn/workspaces/{app}/{sid}/` when omitted | The user's actual files. The agent's `Read/Write/Edit` and the workspace tools target this. The frontend file tree renders this. |
| **`workspace`** | always `~/.digitorn/workspaces/{app}/{sid}/` | Daemon-private state: `state.json`, baselines for diff, history, hidden `__sdk__/`, `.app/`, `.digitorn/` namespaces. |

Three glob patterns are **always** routed to the daemon-private
workspace, even when `sync_to_disk: true` is on:

| Glob | Use case |
|------|----------|
| `__sdk__/**` | SDK-private files (preferences, welcome flags, layout state) |
| `.app/**` | Application-private state (per-session bookmarks, drafts) |
| `.digitorn/**` | Daemon metadata (baselines, session history, transient cache) |

```tsx
const fs = useWorkspaceFiles();

// Lands in ~/.digitorn/workspaces/{app}/{sid}/__sdk__/prefs.json,
// NEVER in the user's project — even with sync_to_disk: true.
await fs.writeFile("__sdk__/prefs.json", JSON.stringify({ tab: "files" }));

// Lands in the user's workdir (their actual project tree).
await fs.writeFile("src/App.tsx", code);
```

The same routing applies to the agent's `Ws*` tools and to the
filesystem module's `Read/Write/Edit/Glob/Grep` — see
[`tools.modules.workspace.config.hidden_paths`](41-preview.md) and
[Filesystem reference](../modules/reference/filesystem.md) to extend the
list per-app.

`useSessionMeta` exposes both paths:

```tsx
const { workspace, workdir } = useSessionMeta();
// workspace: agent-facing path (frontend should render this)
// workdir:   same value (explicit alias for newer clients)
```

The `daemon_workspace` field on the snapshot's `session` block is the
diagnostic / SDK-internal path. **Frontends should never render files
from `daemon_workspace`** — those are internal state.

---

## Host ↔ iframe protocol

Defined in `packages/digitorn-preview-sdk/src/host.ts`. All messages
are namespaced with the `digi:` prefix.

### Host pushes (theme, locale, abort, resize)

```ts
type ClientBoundMessage =
  | { type: "digi:theme-change"; theme: HostTheme }
  | { type: "digi:locale-change"; locale: string }
  | { type: "digi:abort"; reason?: string }
  | { type: "digi:resize"; width: number; height: number };
```

Listen via `useHostMessage` or `onHostMessage`:

```tsx
useHostMessage("digi:theme-change", ({ theme }) => {
  document.documentElement.dataset.theme = theme.mode;  // 'dark' | 'light' | 'auto'
});
```

### Iframe asks the host (open file, toast, navigate)

```ts
sendToHost({ type: "digi:request-toast", message: "Saved", level: "success" });

requestOpenFile("src/App.tsx", 42);          // open in host editor at line 42
requestFocusLine("src/App.tsx", 42, 8);      // line + column
requestToast("Build done", "success");
notifyReady();                                // call once when the iframe is ready
```

`HostBoundMessage` is the union of every message the iframe can send.
The host (Digitorn workbench, Flutter app, custom integrator) decides
which ones to honour.

### Theme bootstrap from URL

The host can pre-set theme via URL query params on the iframe (saves a
flicker before the first `digi:theme-change`):

```
/api/apps/my-app/preview/?theme=dark&accent=%23ff0066&locale=fr
```

`readHostTheme()` resolves these (or falls back to defaults), and
`useHostTheme()` keeps the value live across `digi:theme-change`
events.

---

## Auth & session resolution

When the workbench opens the iframe at
`/api/apps/{app_id}/preview/?session_id={sid}&token={jwt}`, the SDK reads:

1. `session_id` from the iframe's own query string, falling back to the
   parent window's query string.
2. `token` from the same source — passed as the WebSocket `auth`
   payload AND as the `Authorization: Bearer …` header on every REST
   call (`useWorkspaceFiles`, `useSessionMeta`, etc.).
3. `appId` from the URL path (`/api/apps/{appId}/...`).
4. `baseUrl` from `window.location.origin`.

Cross-origin parents are handled gracefully — if the iframe can't read
`window.parent.location` it falls back to its own params and logs a
warning when no `session_id` is found.

---

## Bundled apps — auto-attach

An app is **bundled** when its install dir contains `web/dist/index.html`.
The daemon then:

1. Serves the dist directory at `/api/apps/{app}/web-static/`.
2. Returns `{ type: "bundled", url: ".../web-static/index.html" }` from
   `GET /api/apps/{app}/web-preview?session_id=...&name=default`.
3. The workbench reads that lookup and points its preview iframe at the
   returned URL — no `PreviewProxy` tool call required.

Bundled apps don't need a Node child process at runtime. CSP and auth
allow-paths are tightened so the iframe can load assets, connect to
Socket.IO, and reach the workspace REST routes.

`/api/apps/*/web-static/*` is in the auth middleware's allow-list (the
browser can't attach the bearer token to `<script src>` requests). The
SDK then re-attaches the token as it makes its own API calls.

---

## REST endpoints the SDK uses

| Method | Path | What `useWorkspaceFiles` does |
|--------|------|---|
| `GET` | `/sessions/{sid}/workspace/files/{path}` | `readFile(path)` |
| `PUT` | `/sessions/{sid}/workspace/files/{path}` | `writeFile(path, content, opts)` |
| `DELETE` | `/sessions/{sid}/workspace/files/{path}` | `deleteFile(path)` |
| `POST` | `/sessions/{sid}/workspace/upload/{path}` (multipart) | `uploadFile(path, blob, opts)` |
| `POST` | `/sessions/{sid}/workspace/files/approve` | `approveFile(path)` |
| `POST` | `/sessions/{sid}/workspace/files/reject` | `rejectFile(path)` |
| `POST` | `/sessions/{sid}/workspace/commit` | `commit(message, opts)` |

Snapshot / metadata:

| Method | Path | Used by |
|--------|------|---|
| `GET` | `/sessions/{sid}/preview` | `<DigiPreview>` mount + `useSessionMeta` |
| `POST` | `/sessions/{sid}/workspace/snapshot/export` | `useWorkspaceSnapshot.exportSnapshot` |
| `POST` | `/sessions/{sid}/workspace/snapshot/import` | `useWorkspaceSnapshot.importSnapshot` |
| `POST` | `/sessions/{sid}/workspace/snapshot/fork` | `useWorkspaceSnapshot.forkSnapshot` |

Every call carries `Authorization: Bearer {token}` resolved from the
iframe URL.

---

## Worked examples

### A first-visit wizard

```tsx
import {
  DigiPreview, useSessionLifecycle, useSessionMeta, useWorkspaceFiles,
} from "@digitorn/preview-sdk";

function Welcome() {
  const meta = useSessionMeta();
  const fs = useWorkspaceFiles();

  useSessionLifecycle({
    onFirstVisit: async () => {
      await fs.writeFile(
        "__sdk__/welcome.json",
        JSON.stringify({ shown_at: Date.now() }),
        { autoApprove: true },
      );
    },
  });

  if (meta.isFirstVisit) {
    return <FirstVisitTour appName={meta.appId} />;
  }
  return <Dashboard idleFor={meta.idleSeconds} />;
}

createRoot(document.getElementById("root")!).render(
  <DigiPreview><Welcome /></DigiPreview>,
);
```

### A settings panel that persists into `__sdk__/`

```tsx
const SETTINGS_PATH = "__sdk__/settings.json";

function SettingsPanel() {
  const settings = useFileJson<{ theme: string }>(SETTINGS_PATH) ?? {
    theme: "dark",
  };
  const fs = useWorkspaceFiles();

  return (
    <select
      value={settings.theme}
      onChange={async e => {
        await fs.writeFile(
          SETTINGS_PATH,
          JSON.stringify({ ...settings, theme: e.target.value }),
          { autoApprove: true },   // skip the approve/reject loop
        );
      }}
    >
      <option value="dark">Dark</option>
      <option value="light">Light</option>
    </select>
  );
}
```

The file lands under `~/.digitorn/workspaces/{app}/{sid}/__sdk__/settings.json`
— the user's project never sees it, even with `sync_to_disk: true`.

### A drag-drop image uploader

```tsx
function UploadZone() {
  const fs = useWorkspaceFiles();
  return (
    <input
      type="file"
      accept="image/*"
      onChange={async e => {
        const file = e.target.files?.[0];
        if (!file) return;
        await fs.uploadFile(`assets/${file.name}`, file, { autoApprove: true });
        sendToHost({
          type: "digi:request-toast",
          message: `Uploaded ${file.name}`,
          level: "success",
        });
      }}
    />
  );
}
```

### A read-only editor that follows the agent

```tsx
import { useFile, useAgentStatus, useToolCalls } from "@digitorn/preview-sdk";

function LiveEditor({ path }: { path: string }) {
  const content = useFile(path) ?? "";
  const status = useAgentStatus();

  return (
    <div data-busy={status !== "idle"}>
      <Monaco value={content} readOnly />
      <footer>{status}</footer>
    </div>
  );
}
```

Every time the agent runs `WsWrite` / `WsEdit` against `path`, Socket.IO
pushes a `preview:resource_set` event, the reducer updates the `files`
channel, and the editor re-renders with the new content. No polling.

---

## Cross-references

- [Workspace & Preview](41-preview.md) — `ui.workspace` + `web_preview`
  module + the agent's six `Ws*` tools.
- [Client Manifest](44-client-manifest.md) — what the daemon exposes to
  the workbench (theme, features, slash commands).
- [Bundle namespaces](38-bundle-namespaces.md) — `{{prompt.X}}`,
  `{{include:}}`, hot reload — relevant when the bundled SDK app uses
  daemon-side prompt fragments.
- [API Integration](14-api-integration.md) — full REST + Socket.IO
  contract under `/api/apps/{app}/...`.
- [LSP Diagnostics](27-lsp.md) — the source of `useDiagnostics` events.
- Source: [`packages/digitorn-preview-sdk/src/`](../../packages/digitorn-preview-sdk/src/).
