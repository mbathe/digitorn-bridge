---
id: api-integration
---

# API Integration

The Digitorn daemon exposes a REST + Socket.IO API. Everything
the Flutter / web client does - deploy apps, drive sessions,
stream events, approve tools, manage installs, configure
credentials, run background activations - is HTTP. This page
documents the canonical surfaces; every endpoint cited maps to
a real route in `packages/digitorn/core/api/`.

> **Two big shifts vs. older docs.** The legacy `/chat` and
> `/chat/stream` routes are **gone**. All foreground
> interaction is now **session-based**: create a session →
> POST messages → listen on **Socket.IO** for events. SSE has
> been replaced by Socket.IO across the board.

## Daemon lifecycle

```bash
digitorn start            # default 127.0.0.1:8000
digitorn start --host 0.0.0.0 --tls-cert cert.pem --tls-key key.pem
digitorn start --workers 4
digitorn stop
```

See [Production Deployment](36-production.md) for TLS, auth,
sandbox, CORS.

## Standard response envelope

Most JSON routes return `AppResponse`:

```jsonc
{ "success": true, "data": { ... }, "error": null }
```

On failure, `success: false` and `error` carries a structured
message. The classification fields (`code`, `category`,
`retry`) come from `_classify_error` (`apps.py`); see
[Error classification](#error-classification) below.

## Router map

`server.py` mounts the routers. Prefixes:

| Router | Prefix | Source |
|--------|--------|--------|
| Apps (lifecycle, sessions, messages, ...) | `/api/apps` | `apps_v2/__init__.py` |
| Apps install / upgrade / uninstall | `/api/apps` | `apps_install.py` |
| Discovery (modules, triggers, templates, compile) | `/api/discovery` | `discovery.py` |
| Credentials | `/api` (mixed paths) | `credentials.py` |
| MCP (search, install, pool, OAuth) | `/api/mcp` | `mcp.py` |
| Hub (marketplace) | `/api/hub` | `hub.py` |
| Auth | `/auth` (proxied) | `_auth_redirect_router` (`server.py`) |
| Health | `/health`, `/healthz`, `/readyz` | `server.py` |

## App lifecycle

Routes under `/api/apps`. Lifecycle:
`apps_v2/lifecycle.py`.

| Method | Path | Source | Description |
|--------|------|--------|-------------|
| `GET`  | `/api/apps` | `:listing in apps_v2` | List deployed apps the caller can see (filtered by JWT identity + scope). |
| `GET`  | `/api/apps/{app_id}` | `:264` | Full app summary (manifest, agents, modules, ui, payload_schema). |
| `GET`  | `/api/apps/{app_id}/manifest` | `:245` | Compiled manifest + tool index summary. |
| `POST` | `/api/apps/deploy` | `:358` | Deploy from a daemon-side YAML path. |
| `POST` | `/api/apps/deploy/upload` | `:450` | Deploy from a multipart YAML upload. |
| `POST` | `/api/apps/validate` | `:675` | Compile + validate a YAML, no deploy. |
| `POST` | `/api/apps/{app_id}/disable` | `:713` | Disable (kept in DB; triggers skip it). |
| `POST` | `/api/apps/{app_id}/enable` | `:762` | Enable. |
| `POST` | `/api/apps/{app_id}/reload` | `:981` | Reload from current bundle. |
| `POST` | `/api/apps/{app_id}/run` | `:1049` | One-shot execution (mode=`one_shot`). |
| `POST` | `/api/apps/{app_id}/pipeline` | `:1078` | Pipeline mode entry. |
| `DELETE` | `/api/apps/{app_id}` | `:803` | Undeploy (stops agents, cancels approvals, removes app). |

Deploy request body
(`DeployRequest` at `apps_v2/_shared.py`):

```json
{
  "yaml_path": "/abs/path/to/app.yaml",
  "force": false,
  "scope": "user"
}
```

`scope` defaults to `"user"`; `"system"` requires admin
([Multi-Tenant App Installs](45-multi-tenant.md)).

## Sessions

`apps_v2/sessions.py`. The session is the **unit of state** -
context, memory, todos, workspace, hooks. Every foreground
turn happens inside a session.

| Method | Path | Source | Description |
|--------|------|--------|-------------|
| `POST` | `/api/apps/{app_id}/sessions` | `:116` | Create a session. Body: `name`, `metadata`, `workspace`. |
| `GET`  | `/api/apps/{app_id}/sessions` | `:366` | List sessions visible to caller. |
| `GET`  | `/api/apps/{app_id}/sessions/search` | `:400` | Search across session content. |
| `GET`  | `/api/apps/{app_id}/sessions/{sid}` | `:480` | Session metadata (status, tokens, last_active, ...). |
| `DELETE` | `/api/apps/{app_id}/sessions/{sid}` | `:596` | Delete session + history. |
| `POST` | `/api/apps/{app_id}/sessions/{sid}/fork` | `:615` | Fork conversation at a turn. |
| `POST` | `/api/apps/{app_id}/sessions/{sid}/abort` | `:647` | Cancel the current turn (full cleanup - kills sub-agents, shell tasks, watchers; injects synthetic `interrupted: true` results for orphaned tool calls on resume). |
| `POST` | `/api/apps/{app_id}/sessions/{sid}/resume` | `:807` | Resume an interrupted session (synthetic `interrupted: true` results injected for orphaned tool calls). |
| `POST` | `/api/apps/{app_id}/sessions/{sid}/undo` | `:885` | Roll the conversation back N turns. |
| `POST` | `/api/apps/{app_id}/sessions/{sid}/compact` | `:928` | Force a context compaction now. |
| `GET`  | `/api/apps/{app_id}/sessions/{sid}/export` | `:978` | Export the session as JSON / markdown. |
| `GET`  | `/api/apps/{app_id}/sessions/{sid}/history` | `:1083` | Full message log (system / user / assistant / tool, with tool calls). |
| `GET`  | `/api/apps/{app_id}/sessions/{sid}/state` | `:1826` | Current runtime state (turn, busy, last activity). |
| `GET`  | `/api/apps/{app_id}/sessions/{sid}/memory` | `:1408` | Memory snapshot (goal + facts + episodes + todos). |
| `GET`  | `/api/apps/{app_id}/sessions/{sid}/preview` | `:1449` | Preview state (canvas / files). |
| `GET`  | `/api/apps/{app_id}/sessions/{sid}/queue` | `:1475` | Pending messages in the queue. |
| `GET`  | `/api/apps/{app_id}/sessions/{sid}/images/{image_id}` | `:1384` | Serve a session-scoped image (for vision models). |

### Send a message

`apps_v2/messages.py`:

```http
POST /api/apps/{app_id}/sessions/{sid}/messages
Content-Type: application/json
Authorization: Bearer <jwt>

{
  "message": "Analyze the project",
  "images": [
    {"data": "<base64>", "mime": "image/png", "name": "screenshot.png"}
  ]
}
```

Returns **202 Accepted** immediately. The agent runs the turn
asynchronously and emits results via Socket.IO (next section).

### Workspace endpoints (validation, hunks, writeback, commit)

`apps_v2/workspace.py`. For apps using the `workspace` module
(virtual filesystem). Full list in
[Workspace & Preview](41-preview.md).

## Real-time: Socket.IO

The daemon runs a Socket.IO server side-by-side with FastAPI
(`server.py` `create_socketio_server`,
`socketio_bus.py`). Defaults
(`socketio_bus.py`):

| Setting | Value |
|---------|-------|
| `ping_interval` | 25 s |
| `ping_timeout` | 10 s |
| `max_http_buffer_size` | 1 000 000 (1 MB) |

### Connection

```js
import { io } from "socket.io-client";

const socket = io("http://localhost:8000", {
  auth: { token: "<jwt>" },
  transports: ["websocket"]
});

socket.emit("subscribe", {
  app_id: "my-app",
  session_id: "sess-001"
});
```

The server validates the JWT, joins the client to the
session's room, and starts forwarding events.

### Event types

Emitted by `session_event_bus`. Common events the client must
handle:

| Event | Payload | Trigger |
|-------|---------|---------|
| `turn_start` / `turn_end` | `{turn, ts}` | Beginning + end of an agent turn. |
| `tool_call` | `{call_id, name, params, image_data?, image_mime?}` | Agent decided to call a tool. |
| `tool_result` | `{call_id, success, data, error}` | Tool returned. |
| `thinking_started` / `thinking_delta` | `{text}` | Progressive thinking blocks (Claude). |
| `assistant_delta` / `assistant_message` | `{text}` | Streaming assistant text. |
| `error` | `_classify_error()` payload | Turn-level error. |
| `hook` | `{event, action, ...}` | A YAML hook fired. |
| `approval_request` | `{request_id, tool, params, timeout}` | Tool needs explicit approval. |
| `notification` | `{title, body, ...}` | Background task completed. |
| `widget:*` | Widget tree events | UI declarative widgets. |
| `preview:state_changed` | Preview state | Workspace / canvas update. |
| `agent_event` | `{type: spawn_agent / agent_progress / agent_result / agent_cancel, agent_id, ...}` | Sub-agent lifecycle. |
| `abort` | `{reason}` | Session abort triggered. |

> **Event filtering.** The client must filter events by
> `session_id` before displaying - events from one session
> arrive on the same room as the active one only when both
> share the room name.

## Approvals

`apps_v2/approvals.py`.

| Method | Path | Source | Description |
|--------|------|--------|-------------|
| `GET`  | `/api/apps/{app_id}/approvals` | `:115` | List pending approval requests. |
| `POST` | `/api/apps/{app_id}/approve` | `:131` | Resolve one. Body: `{request_id, approved, message}`. |

Lifecycle:

1. Tool call hits a `capabilities.approve:` rule.
2. Daemon enqueues an `ApprovalRequest`, emits an
   `approval_request` Socket.IO event, and parks the tool.
3. Client displays the request, user picks
   approve / deny / approve+message.
4. Client `POST /approve` with the decision.
5. Tool resumes (or returns `denied`). Default timeout
   `capabilities.approval_timeout` (`schema.py`,
   default 300 s, range 30 - 3600).

## Background

`apps_v2/background.py`. Two distinct surfaces:

### Background **tasks** - fire-and-forget tool runs

| Method | Path | Source | Description |
|--------|------|--------|-------------|
| `POST` | `/api/apps/{app_id}/background-tasks` | `:197` | Launch `{tool, params}`. |
| `GET`  | `/api/apps/{app_id}/background-tasks` | `:115` | List tasks. |
| `GET`  | `/api/apps/{app_id}/sessions/{sid}/tasks` | `:133` | Tasks scoped to a session. |
| `GET`  | `/api/apps/{app_id}/background-tasks/{tid}` | `:177` | Task status. |
| `POST` | `/api/apps/{app_id}/background-tasks/{tid}/wait` | `:227` | Block until done (default timeout 60 s). |
| `POST` | `/api/apps/{app_id}/sessions/{sid}/tasks/{tid}/cancel` | `:251` | Cancel. |
| `DELETE` | `/api/apps/{app_id}/background-tasks/{tid}` | `:275` | Delete record. |
| `GET`  | `/api/apps/{app_id}/notifications/active` | `:297` | Lightweight poll for clients without Socket.IO. |
| `POST` | `/api/apps/{app_id}/notifications` | `diag.py` | Drain pending notifications + trigger an agent turn to process them. |

### Background **sessions** - multi-user trigger-driven apps

Documented in [Background Sessions](38-background-sessions.md).
Routes under `/api/apps/{app_id}/background-sessions/...` and
`/api/apps/{app_id}/activations/...`.

### Artifacts

| Method | Path | Source | Description |
|--------|------|--------|-------------|
| `GET`  | `/api/apps/{app_id}/artifacts/{event_id}/download` | `:751` | Download a file the agent produced (resolved by activation event id). |
| `HEAD` | same | `:852` | Size + mime probe. |

## Quotas (rate limits)

`apps_v2/quota.py`. Default per-app quota is
`server.rate_limit_rpm` (`config.py`) - **100 000 RPM** by
default (effectively off). Admin / auth / deploy buckets get
half (`server.py`). Per-app and per-user overrides:

| Method | Path | Source | Description |
|--------|------|--------|-------------|
| `GET`  | `/api/apps/{app_id}/quota` | `:115` | Current bucket usage + cap. |
| `PUT`  | `/api/apps/{app_id}/quota` | `:158` | Set custom rpm. Body: `{rpm}`. |
| `DELETE` | `/api/apps/{app_id}/quota` | `:205` | Reset to default. |
| `GET`  | `/api/apps/{app_id}/quota/me` | `:347` | The caller's own quota usage. |
| `GET`  | `/api/apps/{app_id}/quota/user/{user_id}` | `:233` | Per-user (admin). |
| `PUT`  | `/api/apps/{app_id}/quota/user/{user_id}` | `:270` | Set per-user. |
| `DELETE` | `/api/apps/{app_id}/quota/user/{user_id}` | `:314` | Reset per-user. |

## Install / upgrade / uninstall (multi-tenant)

`apps_install.py`. Composes with the routes above to provide
the canonical install lifecycle for the marketplace +
self-installs.

| Method | Path | Source | Description |
|--------|------|--------|-------------|
| `GET`  | `/api/apps/{app_id}/check-update` | `:73` | Latest version available vs installed. |
| `POST` | `/api/apps/install` | `:139` | Install a package. Body includes `source_type` (`yaml` / `package` / `hub` / ...), `source_uri`, `scope`, `accept_permissions`. |
| `POST` | `/api/apps/{app_id}/upgrade` | `:243` | Preserves the existing `(scope, owner_user_id)` tuple. |
| `POST` | `/api/apps/{app_id}/uninstall` | `:340` | Targets the matching install row (per scope). |

Errors raised:

- `409 PackageIdCollision` - same `app_id` already installed at
  this `(scope, owner)`.
- `409 PermissionsRequired` - request must be retried with
  `accept_permissions: true` after surfacing the permission
  list.
- `403` - non-admin tried `scope=system`.

## Discovery

`discovery.py`. Power the marketplace + Builder canvas - list
modules, list trigger types, render templates, compile YAML.

| Method | Path | Source | Description |
|--------|------|--------|-------------|
| `GET`  | `/api/discovery/modules` | `:137` | All modules + their action manifests. |
| `GET`  | `/api/discovery/modules/{module_id}` | `:167` | Detail. |
| `GET`  | `/api/discovery/triggers` | `:377` | Trigger types + schemas. |
| `GET`  | `/api/discovery/triggers/configured` | `:396` | Triggers configured across deployed apps. |
| `GET`  | `/api/discovery/templates` | `:481` | App templates. |
| `GET`  | `/api/discovery/templates/{template_id}` | `:525` | One template. |

All discovery endpoints require a Bearer token like every other
`/api/*` route - the daemon does not expose a loopback bypass for
in-process agent self-calls (see
[Production Deployment → In-process agent calls and auth](36-production.md#in-process-agent-calls-and-auth)).

## Credentials

`credentials.py`. Full reference + scope semantics in
[credentials.md](../reference/runtime/credentials.md). Key endpoints:

| Method | Path | Source | Description |
|--------|------|--------|-------------|
| `GET`  | `/api/apps/{app_id}/credentials/manifest` | `:209` | Slot manifest the app needs filled. |
| `GET`  | `/api/apps/{app_id}/credentials/schema` | `:351` | Declarative `credentials_schema` from the YAML. |
| `GET`  | `/api/apps/{app_id}/credentials/{slot}` | `:398` | Resolved credential metadata for a slot. |
| `PUT`  | `/api/apps/{app_id}/credentials/{slot}` | `:431` | Bind a credential id to a slot. |
| `DELETE` | `/api/apps/{app_id}/credentials/{slot}` | `:548` | Unbind. |
| `GET`  | `/api/users/me/credentials` | `:572` | Caller's credentials (decrypted only at injection time). |
| `GET`  | `/api/credentials` | `:1416` | List credentials the caller can manage. |
| `POST` | `/api/credentials` | `:1437` | Create a credential. |
| `GET`  | `/api/credentials/{cid}` | `:1469` | Detail. |
| `PUT`  | `/api/credentials/{cid}` | `:1486` | Update. |
| `DELETE` | `/api/credentials/{cid}` | `:1529` | Revoke + audit log. |
| `POST` | `/api/credentials/{cid}/refresh` | `:1564` | Trigger OAuth refresh. |
| `POST` | `/api/credentials/test` | `:1665` | Live-connection test for a draft credential. |
| `POST` | `/api/credentials/{cid}/grants` | `:1776` | Grant access (per-app / per-team). |
| `GET`  | `/api/credentials/providers` | `:1332` | TOML provider catalog (16 builtins). |

A health endpoint `GET /api/credentials-health` reports
master_key, cipher, audit chain, OAuth registry, and refresh
loop state.

## OAuth (per-app, MCP)

`apps_v2/oauth_mcp.py`.

| Method | Path | Source | Description |
|--------|------|--------|-------------|
| `GET`  | `/api/apps/{app_id}/oauth/authorize` | `:115` | Start an OAuth flow. |
| `GET`  | `/api/apps/{app_id}/oauth/callback` | `:169` | OAuth callback. |
| `GET`  | `/api/apps/{app_id}/mcp/pending-oauth` | `:418` | Pending OAuth requests for the app. |
| `POST` | `/api/apps/{app_id}/mcp/{server_id}/oauth-token` | `:257` | Inject a token (server-to-server). |
| `DELETE` | `/api/apps/{app_id}/mcp/{server_id}/oauth-token` | `:325` | Revoke. |

## Secrets (per-app, encrypted)

`apps_v2/secrets.py`. Per-app legacy secret storage. Prefer
the credentials system for new apps.

| Method | Path | Source | Description |
|--------|------|--------|-------------|
| `GET`  | `/api/apps/{app_id}/secrets` | `:330` | List keys (no values). |
| `GET`  | `/api/apps/{app_id}/secrets/{key}` | `:339` | `{exists: bool}`. |
| `PUT`  | `/api/apps/{app_id}/secrets` | `:351` | Bulk set. |
| `PUT`  | `/api/apps/{app_id}/secrets/{key}` | `:421` | Set one. Body: `{value}`. |
| `DELETE` | `/api/apps/{app_id}/secrets/{key}` | `:471` | Delete. |
| `GET`  | `/api/apps/{app_id}/required-secrets` | `:115` | Secret keys the YAML references. |

## LSP (language-server proxy)

`apps_v2/lsp.py`.

| Method | Path | Source | Description |
|--------|------|--------|-------------|
| `POST` | `/api/apps/{app_id}/sessions/{sid}/lsp/rpc` | `:115` | LSP JSON-RPC pass-through. |
| `POST` | `/api/apps/{app_id}/sessions/{sid}/lsp/cancel` | `:213` | Cancel a pending request. |

## Preview (proxied dev server)

`apps_v2/preview.py`. Documented in
[Workspace & Preview](41-preview.md).

| Method | Path | Source | Description |
|--------|------|--------|-------------|
| `GET`  | `/api/apps/{app_id}/preview-server/status` | `:259` | Dev-server health + readiness. |
| `GET`  | `/api/apps/{app_id}/preview-server/logs` | `:243` | Tail logs. |
| `POST` | `/api/apps/{app_id}/preview-server/restart` | `:301` | Restart. |
| `GET`  | `/api/apps/{app_id}/preview/{path:path}` | `:324` | Reverse-proxy to the dev server (HTTP + WebSocket). |

## Diagnostics + UI helpers

`apps_v2/diag.py`.

| Method | Path | Source | Description |
|--------|------|--------|-------------|
| `GET`  | `/api/apps/{app_id}/diagnostics` | `:115` | Health snapshot of the deployed app. |
| `GET`  | `/api/apps/{app_id}/errors` | `:176` | Recent failed activations / turns. |
| `GET`  | `/api/apps/{app_id}/status` | `:185` | Status summary. |
| `GET`  | `/api/apps/{app_id}/ui-config` | `:305` | Compiled `ui:` block (theme, features, slash commands, ...). |
| `GET`  | `/api/apps/{app_id}/files` | `:369` | Bundle file tree. |
| `GET`  | `/api/apps/{app_id}/icon` | `:454` | Serve the app icon. |
| `GET`  | `/api/apps/{app_id}/index` | `:527` | Tool index. |
| `GET`  | `/api/apps/{app_id}/assets/{path:path}` | `:574` | Bundle asset (resolves `{{asset.X}}`). |
| `GET`  | `/api/apps/{app_id}/channels/health` | `:699` | `channels` module health. |
| `GET`  | `/api/apps/{app_id}/deploy-status` | `:822` | Deploy phase + last error. |
| `GET`  | `/api/apps/{app_id}/payload-schema` | `:951` | The `runtime.payload_schema` (background apps). |

## Health + metrics (daemon-level)

`server.py`. Not scoped to an app:

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/health` | `{"status": "ok"}`. |
| `GET`  | `/healthz` | 200 (Kubernetes liveness). |
| `GET`  | `/readyz` | 200 (Kubernetes readiness). |

Operational metrics are exposed through admin-only HTTP
endpoints (operational reference held by the daemon
administrator).

## MCP management (daemon-level)

`/api/mcp/...`. See [Skills & MCP](04d-mcp.md) and
[modules/reference/mcp.md](../reference/modules/mcp.md).

## Error classification

`apps.py::_classify_error`. Every error surfaced via
Socket.IO or REST is run through this classifier:

```json
{
  "error": "Insufficient balance",
  "code": "billing_402",
  "category": "billing",
  "retry": false,
  "detail": "Top up your DeepSeek account."
}
```

Categories: `billing`, `auth`, `rate_limit`, `provider`,
`network`, `internal`. The Flutter / web client uses
`category` to pick the right banner (red for billing, amber for
rate limit, retry button for network).

## Authentication

JWT-based. The daemon does **not** sign tokens - it consumes
them from a central `digitorn-auth` service whose JWKS public
key it pulls at startup. See [Auth](22-auth.md) for the full
flow.

Send the token in `Authorization: Bearer <jwt>` on every
request and pass it as `auth.token` to Socket.IO.

There is no daemon-side loopback bypass: calls from `127.0.0.1`
still need a Bearer token on every `/api/*` path. See
[Production Deployment → In-process agent calls and auth](36-production.md#in-process-agent-calls-and-auth).

## Canonical SDK pattern

```js
// 1. Open a Socket.IO connection (kept open for the session lifetime)
const socket = io("http://localhost:8000", {
  auth: { token: jwt },
  transports: ["websocket"]
});

// 2. Create a session
const { data: session } = await fetch(
  `/api/apps/my-app/sessions`,
  { method: "POST", headers: hdrs,
    body: JSON.stringify({ name: "demo", workspace: "/path/to/proj" }) }
).then(r => r.json());

// 3. Subscribe to its room
socket.emit("subscribe", { app_id: "my-app", session_id: session.id });

// 4. Wire event handlers
socket.on("tool_call",        e => render.toolCall(e));
socket.on("tool_result",      e => render.toolResult(e));
socket.on("assistant_delta",  e => render.append(e.text));
socket.on("approval_request", e => ui.askApproval(e).then(decision =>
  fetch(`/api/apps/my-app/approve`, {
    method: "POST", headers: hdrs,
    body: JSON.stringify({ request_id: e.request_id, approved: decision }),
  })
));
socket.on("error", e => render.errorBanner(e));

// 5. Send a message - the response arrives on the socket above
await fetch(`/api/apps/my-app/sessions/${session.id}/messages`, {
  method: "POST", headers: hdrs,
  body: JSON.stringify({ message: "Analyze the project" })
});  // returns 202 immediately

// 6. Tear down when the user navigates away
socket.disconnect();
```

For background apps (cron / webhook / file watch), use the
`background-sessions` + payload routes in
[Background Sessions](38-background-sessions.md) instead.

## CLI parity

The `digitorn` CLI hits these same endpoints. The dev CLI for
testing apps against a running daemon is documented in
[Dev CLI](46-dev-cli.md).

## Cross-references

- Auth flow + JWT cache:
  [Auth](22-auth.md)
- Approval gate semantics + capabilities resolution:
  [Security](11-security.md)
- Multi-tenant install scopes (system vs user):
  [Multi-Tenant App Installs](45-multi-tenant.md)
- Background activations + payload routes:
  [Background Sessions](38-background-sessions.md)
- Workspace + preview routes:
  [Workspace & Preview](41-preview.md)
- Credentials full reference:
  [credentials.md](../reference/runtime/credentials.md)
- Production deployment (TLS, auth, sandbox, rate limits):
  [Production Deployment](36-production.md)
