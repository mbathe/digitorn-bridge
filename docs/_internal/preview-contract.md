# Preview Contract

How any application plugs a custom preview into the Digitorn ecosystem.

The host (Digitorn web client / Flutter desktop / future surfaces) embeds your app's preview as an iframe and provides everything you need: session info, auth token, theme, locale, and a typed bidirectional channel for cross-cutting events. Your app brings whatever rendering it wants — React, Vue, vanilla HTML, a static dist, a streaming agent UI, anything.

This document describes:

1. The URL the host loads
2. The query parameters you receive
3. The `postMessage` protocol (host ↔ iframe)
4. The HTTP endpoints your iframe can call
5. The `@digitorn/preview-sdk` helpers (recommended)
6. YAML configuration

## 1. URL

The host always loads your preview at:

```
/api/apps/{app_id}/preview/?session_id={sid}&token={jwt}&theme={mode}&locale={bcp47}
```

The daemon serves this URL one of two ways:

- **Static dist**: if your app ships a `web/dist/index.html`, the daemon serves it directly. Zero process. Best for production / read-only previews.
- **Dev server proxy**: if `preview.enabled: true` is in your YAML and the manager is `RUNNING`, the daemon reverse-proxies to your Vite/Next/etc. dev server. Live HMR.

When both exist, the dev server wins (so your code edits get HMR even when a stale dist is sitting around).

## 2. Query parameters

Standardised, all optional except `session_id`:

| Param | Type | Description |
|---|---|---|
| `session_id` | string | The active chat session your preview is bound to. Use it as the cache key for everything you fetch. |
| `token` | string | Bearer JWT for authenticating HTTP and Socket.IO calls back to the daemon. Pass via `Authorization: Bearer ${token}` header for HTTP, query string for WS. |
| `theme` | `dark` \| `light` \| `auto` | Initial theme mode. Watch for live changes via the postMessage protocol. |
| `accent` | `#RRGGBB` | Brand accent color set by the host. `null` if the host hasn't customised. |
| `locale` | BCP-47 (e.g. `en`, `fr`, `en-US`) | UI language hint. |

Read these via `URLSearchParams` or `@digitorn/preview-sdk`'s `readSession()` / `readHostTheme()` helpers.

## 3. `postMessage` protocol

All messages are JSON-serialisable objects with a `type` field namespaced under `digi:` so they don't collide with other libraries' messages.

### Host → iframe (you receive)

```ts
type ClientBoundMessage =
  | { type: "digi:theme-change"; theme: { mode, accent, locale } }
  | { type: "digi:locale-change"; locale: string }
  | { type: "digi:abort"; reason?: string }
  | { type: "digi:resize"; width: number; height: number };
```

Wire a `window.addEventListener("message", ...)` filter on the `digi:` prefix, or use the SDK's `useHostMessage(type, handler)` hook which does this for you.

### Iframe → host (you send)

```ts
type HostBoundMessage =
  | { type: "digi:ready" }                          // recommended on mount
  | { type: "digi:request-open-file"; path; line?; column? }
  | { type: "digi:request-focus-line"; path; line; column? }
  | { type: "digi:request-toast"; message; level? }
  | { type: "digi:request-navigate"; route };
```

Send via `window.parent.postMessage(msg, "*")` (browser host) OR via the Flutter native channel `window.DigiHost.postMessage(JSON.stringify(msg))` (Flutter host). The SDK's `sendToHost(...)` picks the right bridge automatically.

The host uses these to drive cross-cutting UI:
- `digi:request-open-file` opens the file in the workspace IDE (Monaco)
- `digi:request-toast` surfaces a notification
- `digi:ready` lets the host stop showing a loading spinner

## 4. HTTP API access

Your iframe is **same-origin** with the daemon (it's served from `/api/apps/{id}/preview/`), so you can call any daemon route directly with the bearer token:

```ts
fetch(`/api/apps/${appId}/sessions/${sessionId}/workspace/files/README.md`, {
  headers: { Authorization: `Bearer ${token}` },
});
```

Useful endpoints:

- `GET /api/apps/{id}/sessions/{sid}/preview` — full session snapshot (state + resources)
- `GET /api/apps/{id}/sessions/{sid}/workspace/files/{path}` — file content + metadata
- `POST /api/apps/{id}/sessions/{sid}/workspace/files/approve` — approve pending changes
- `GET /api/apps/{id}/preview-bootstrap?session_id={sid}` — single-call config dump (alternative to query params)

## 5. The SDK (recommended)

```bash
npm install @digitorn/preview-sdk
```

```tsx
import { DigiPreview, useFile, useAgentStatus, useHostTheme, requestOpenFile } from "@digitorn/preview-sdk";

function MyPreview() {
  const theme = useHostTheme();         // live host theme (auto-updates)
  const readme = useFile("README.md");  // live file content (auto-updates)
  const status = useAgentStatus();      // idle | thinking | working | done

  return (
    <div data-theme={theme.mode}>
      <h1 onClick={() => requestOpenFile("src/App.tsx")}>{status}</h1>
      <pre>{readme}</pre>
    </div>
  );
}

export default function App() {
  return (
    <DigiPreview>
      <MyPreview />
    </DigiPreview>
  );
}
```

The provider:
- Reads `session_id` + `token` from URL query
- Connects Socket.IO to the daemon and joins the session room
- Dispatches every `preview:*` event into a reducer
- Exposes 25+ hooks: `useFile`, `useFiles`, `useFilesByPrefix`, `useNodes`, `useEdges`, `useAgentStream`, `useToolCalls`, `useApprovalRequest`, `useDiagnostics`, etc.

See [@digitorn/preview-sdk](https://www.npmjs.com/package/@digitorn/preview-sdk) for the full API.

## 6. YAML configuration

Three ways to ship a preview, in increasing order of LLM
involvement. The daemon **never spawns a dev server on its own**;
either you ship a static bundle, or the agent runs the dev server
itself at runtime.

### Static dist (declarative, zero LLM action)

Just have a `web/dist/index.html` next to your `app.yaml`. The
daemon picks it up automatically. No YAML changes, no module to
load, no LLM tool call. Best for shells / consumer apps that
bundle in-browser and just need to load their pre-built JS.

### Built-and-served (LLM builds, then attaches static)

```yaml
tools:
  modules:
    web_preview: {}
    workspace:
      config:
        sync_to_disk: true   # required so the build output is on disk
```

In the agent's prompt, instruct it to run `Bash("npm run build")`
then `PreviewStatic(path="web/dist")`. The daemon serves the
directory live; rebuilding is visible on the next page load with
no re-attach. Zero process while idle.

### Live dev server (LLM spawns + attaches with HMR)

```yaml
tools:
  modules:
    web_preview: {}
    workspace:
      config:
        sync_to_disk: true
```

In the agent's prompt, instruct it to run
`Bash("npm run dev", run_in_background=true)`, wait for the
server to bind, then `PreviewProxy(port=5173)`. The daemon
reverse-proxies HTTP from `/api/apps/{id}/preview/` to the agent's
process. Lifecycle is owned by the agent: it picks the port,
resolves conflicts, and the process dies with the session
(via `shell.cleanup_session`).

### Deprecated: `ui.preview:` block

The legacy `ui.preview` YAML block (with `command`, `port`, `cwd`,
`install_command`, `startup_timeout`, `restart_on_crash`, …) is
**ignored at deploy time**. The daemon used to spawn the dev server
automatically; it doesn't anymore. Migrate to one of the three
modes above.

### Workspace render mode

`modules.workspace.config.render_mode` is **informational** — it's a hint to the host about what kind of UI your preview renders, but the host doesn't use it to choose a renderer. The actual rendering is delegated entirely to your preview iframe.

Common values: `react`, `html`, `markdown`, `slides`, `code`, `builder`, `auto`. Auto detects from the first written file's extension.

## 7. Testing your preview

Run your dev server standalone:

```bash
cd ./web && npm run dev
```

Then point a browser at `http://localhost:5173/?session_id=test_session&token=dev_token` to verify the SDK reads the query params correctly.

When testing through the daemon, the URL is `/api/apps/{your-app-id}/preview/`. The daemon will append `?session_id=` and `?token=` automatically based on the active chat session.

## 8. Security model

- The token is a real JWT. Treat it as a bearer credential — don't log it, don't paste it into share URLs.
- The iframe is sandboxed by the host (`allow-scripts allow-same-origin allow-forms`). If you need additional capabilities (camera, mic, payment), negotiate via postMessage rather than expecting them to work out of the box.
- The host filters all incoming messages by the `digi:` prefix. Other libraries' postMessages are ignored.

## 9. Versioning

The contract follows semantic versioning. Adding new optional message types or query params is a minor bump. Removing or repurposing existing ones is a major bump and triggers a deprecation cycle (the SDK warns at runtime for one minor before removal).

Current contract version: **1.0**.
