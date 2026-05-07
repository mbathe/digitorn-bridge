---
id: api-internal
title: Internal HTTP API (NEVER PUBLISH)
---

# Internal HTTP API surfaces

> **PRIVATE - NEVER PUBLISH.** This page is excluded from the
> Docusaurus build via `docs.exclude` in `docusaurus.config.js`.
> It documents endpoints intended for the daemon's own admin
> tooling, internal builder UI, and ops dashboards.
>
> Exposing these URLs in public documentation would help an
> attacker map the surface, even though every endpoint requires
> a real Bearer token. Keep this file in `_internal/`.

All routes below sit on the same daemon as the public API and
are protected by JWT auth + role checks.

## Admin (system-wide)

Require `is_admin: true` on the JWT.

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/admin/credentials` | GET/POST | List / create system-scope credentials. |
| `/api/admin/credentials/{cid}` | DELETE | Delete a system-scope credential. |
| `/api/admin/credentials/audit/verify` | POST | Verify the hash-chained audit log integrity. |
| `/api/admin/quotas` | GET/POST | List / upsert system-wide quotas. |
| `/api/admin/quotas/{qid}` | DELETE | Delete system-wide quota. |
| `/api/admin/package-events` | GET | Inspect the package-install event log. |

## Builder (draft management)

The internal builder UI uses these to keep work-in-progress
YAMLs out of the deployed-apps store.

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/builder/drafts` | GET | List drafts. |
| `/api/builder/drafts` | POST | Create draft. |
| `/api/builder/drafts/{id}` | GET | Get draft. |
| `/api/builder/drafts/{id}` | PATCH | Update draft. |
| `/api/builder/drafts/{id}` | DELETE | Delete draft. |
| `/api/builder/drafts/{id}/deploy` | POST | Compile + deploy a draft. |

## Discovery helpers (builder-only)

The builder needs to validate YAML without deploying. Public
users go through `/api/apps/validate` instead.

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/discovery/compile` | POST | Validate YAML without deploying. |
| `/api/discovery/prompt-preview` | POST | Preview a system prompt with variables resolved. |
| `/api/discovery/generate-package-manifest` | POST | Auto-generate `package.toml` from a YAML. |

## Module direct execution

The `/execute` endpoint lets admin tools run a module action
without going through the agent loop.

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/modules/{mid}/execute` | POST | Execute a module action directly. **Admin only.** |

(The read-only `/api/modules/*` introspection routes stay public.)

## Daemon runtime config

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/config` | GET | Current effective `Settings` snapshot. |
| `/api/config` | PATCH | Update runtime-tunable fields (rate limit, timeouts, ...). |
| `/api/config/browse` | GET | Filesystem browser helper for the admin UI's path picker. |

`/api/config/browse` was originally written for the desktop
admin UI - it returns a directory listing relative to a root
the admin selected. Auth-gated, but still risky to expose
publicly: knowing the URL plus a stolen token would give
arbitrary directory enumeration.

## Security profiles

Admin-managed capability profiles applied at app deploy time.

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/security/profiles` | POST | Create profile. |
| `/api/security/profiles/{app_id}` | GET/PATCH/DELETE | Manage profile. |
| `/api/security/profiles/{app_id}/grants` | GET | List grants. |
| `/api/security/profiles/{app_id}/grants/{mid}` | PUT/DELETE | Upsert/remove module grant. |

## Requirements (binaries installer)

These trigger package installs (pip / system / npm) on the
daemon host. Should never be exposed to non-admins.

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/requires` | GET | List required packages/binaries + install status. |
| `/api/requires/install` | POST | Install one requirement. |
| `/api/requires/install-all` | POST | Install every missing requirement. |

## Metrics

Operational metrics for ops dashboards / Prometheus scrape.
The public site exposes `/healthz` (alive ping) only.

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/metrics` | GET | JSON metrics snapshot. |
| `/api/metrics/prometheus` | GET | Prometheus-format metrics. |
| `/api/metrics/sessions` | GET | Active session counters. |
| `/api/metrics/sessions/{session_id}` | GET | Per-session metrics. |
| `/api/metrics/apps/{app_id}` | GET | Per-app metrics. |

## UI Helpers

Internal UI affordances. Public clients shouldn't depend on
their shape.

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/ui/tool_display_defaults` | GET | Default display settings per tool (icons, silent flags). |

---

## Public-facing endpoints (also private now per user request)


All daemon HTTP endpoints under `/api/`. Auth via JWT `Authorization: Bearer <token>`
header, except where noted. There is **no loopback bypass** for `/api/*` - calls from
`127.0.0.1` still require a Bearer token (see [Production Deployment → In-process
agent calls and auth](../../language/36-production.md#in-process-agent-calls-and-auth)).

> **See also**:
> - [API Integration](../../language/14-api-integration.md) - comprehensive REST + Socket.IO surface with every endpoint cited to its `apps_v2/` source file.
> - [Socket.IO Protocol](SOCKETIO.md) - real-time event stream.
> - [credentials.md](../runtime/credentials.md) - full credential lifecycle + 30+ credential endpoints.

## Authentication

| Route | Method | Purpose |
|-------|--------|---------|
| `/auth/login` | POST | Get access_token + refresh_token |
| `/auth/register` | POST | Create user |
| `/auth/refresh` | POST | Refresh access token |
| `/auth/me` | GET | Current user info |
| `/auth/logout` | POST | Invalidate session |
| `/auth/sessions` | GET | List auth sessions for current user |
| `/auth/sessions/{sid}` | DELETE | Revoke auth session |
| `/auth/sessions/{sid}/history` | GET | Auth session history |
| `/auth/sessions/{sid}/fork` | POST | Fork auth session |

## Discovery

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/discovery/modules` | GET | List all loaded modules |
| `/api/discovery/modules/{id}` | GET | Module details + actions |
| `/api/discovery/triggers` | GET | Available trigger types |
| `/api/discovery/templates` | GET | List starter templates |
| `/api/discovery/templates/{id}` | GET | Template YAML |

## Apps (deployment)

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/apps` | GET | List deployed apps. `?include_disabled=true` (admin-only) appends disabled apps for management. |
| `/api/apps/{id}` | GET | App details + summary |
| `/api/apps/{id}` | DELETE | Undeploy app |
| `/api/apps/validate` | POST | Validate YAML |
| `/api/apps/deploy` | POST | Deploy YAML |
| `/api/apps/deploy/upload` | POST | Deploy from uploaded file |
| `/api/apps/{id}/run` | POST | Run one-shot app |
| `/api/apps/{id}/pipeline` | POST | Execute pipeline |
| `/api/apps/{id}/reload` | POST | Hot-reload app (no daemon restart) |
| `/api/apps/{id}/status` | GET | App status |
| `/api/apps/{id}/reload` | POST | Re-read bundle + secrets |
| `/api/apps/{id}/disable` | POST | Disable app - hides + refuses use; reversible by admin |
| `/api/apps/{id}/enable` | POST | **Admin-only** - re-enable a disabled app |

### Deletion semantics

`DELETE /api/apps/{id}` accepts:

| Query param | Default | Effect |
|---|---|---|
| `undeploy_only=true` | `false` | Stop in memory, keep everything persistent. Reloads on next restart. |
| `delete_history=false` | `true` | Remove app + bundles + disk, but KEEP sessions/messages/activations (Application row kept with `disabled=true`). Not reversible via enable (no bundle). Use this when you want audit trail. |
| `scope=system` | (auto) | **Admin-only** override - target the system-scope install even when a user install exists. |

Default (no query params) = **total deletion of the caller's scope**: the JWT's user_id picks the user-scoped install if it exists, otherwise the system install. Memory + every bundle on disk for THIS scope + every DB row scoped to it. Other scopes of the same `app_id` are untouched.

### Multi-tenant scoping

Each install is uniquely identified by the composite key
`(app_id, scope, owner_user_id)`:

- `scope="system", owner_user_id=""` - global install, visible to every user.
- `scope="user",   owner_user_id="alice"` - Alice's private install.

The daemon enforces composite uniqueness. Two users can install the same `app_id` alongside a system install without collision. DELETE / disable / enable target exactly one scope - the caller's by default, or an admin-specified one via `?scope=system`.

### Disable vs delete

| | `POST /disable` | `DELETE ?delete_history=false` | `DELETE` (default) |
|---|---|---|---|
| App visible to users | no | no | no |
| App visible to admin | yes (`include_disabled=true`) | yes | no (gone) |
| Sessions/messages kept | yes | yes | no |
| Bundle on disk kept | yes | no | no |
| Reversible by admin | yes (`POST /enable`) | no (bundle gone) | no (everything gone) |

## Sessions

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/apps/{id}/sessions` | GET | List sessions for app |
| `/api/apps/{id}/sessions` | POST | Create session |
| `/api/apps/{id}/sessions/search` | GET | Search sessions |
| `/api/apps/{id}/sessions/{sid}` | GET | Get session |
| `/api/apps/{id}/sessions/{sid}` | DELETE | Delete session |
| `/api/apps/{id}/sessions/{sid}/messages` | POST | Send message (run turn) |
| `/api/apps/{id}/sessions/{sid}/history` | GET | Full history (messages + events + snapshots) |
| `/api/apps/{id}/sessions/{sid}/memory` | GET | Session memory state |
| `/api/apps/{id}/sessions/{sid}/workspace` | GET | Workspace state |
| `/api/apps/{id}/sessions/{sid}/preview` | GET | Preview snapshot |
| `/api/apps/{id}/sessions/{sid}/export` | GET | Export session JSON |
| `/api/apps/{id}/sessions/{sid}/compact` | POST | Compact history |
| `/api/apps/{id}/sessions/{sid}/undo` | POST | Undo last message |
| `/api/apps/{id}/sessions/{sid}/fork` | POST | Fork into new session |
| `/api/apps/{id}/sessions/{sid}/abort` | POST | Abort running turn |
| `/api/apps/{id}/sessions/{sid}/resume` | POST | Resume interrupted session |
| `/api/apps/{id}/sessions/{sid}/tasks` | GET | List bg tasks |
| `/api/apps/{id}/sessions/{sid}/tasks/{task_id}/cancel` | POST | Cancel bg task |
| `/api/apps/{id}/sessions/{sid}/images/{image_id}` | GET | Fetch session image |

### History response shape

`GET /api/apps/{id}/sessions/{sid}/history` returns:

```jsonc
{
  "success": true,
  "data": {
    "session_id": "...",
    "app_id": "...",
    "user_id": "...",
    "messages": [...],           // user/assistant/tool messages
    "events": [...],             // per-turn event log, chronological
    "preview_snapshot": {...},   // workspace files + state (persistent)
    "memory_snapshot": {...}     // memory module state
  }
}
```

Events are aggregated from per-turn SQLite storage - full replay possible.

## Workspace

The workspace surface lives under
`/api/apps/{id}/sessions/{sid}/workspace/`. It backs the
[workspace module](../modules/workspace.md) and the
validation workflow (per-file pending diff, approve / reject,
git_status, commit).

| Route | Method | Purpose |
|-------|--------|---------|
| `/workspace` | GET | Snapshot: state map, files map, channels map. |
| `/workspace/code-snapshot` | GET | File tree + metadata only (no content). Includes validation, language, lines, status, pending-diff flags. |
| `/workspace/changes` | GET | Diff vs baseline across all files in this session - pending hunks per file. |
| `/workspace/files/{path}` | GET | File content. Pass `?include_baseline=true` to also get the last-approved baseline + `unified_diff_pending`. |
| `/workspace/files/{path}` | PUT | User writeback (manual edit, conflict resolution, drag-drop import). Pass `auto_approve: true` in body for single-call bypass. |
| `/workspace/files/{path}` | DELETE | Delete file from session workspace. |
| `/workspace/files/{path}/history` | GET | Revision list (revision, approved_at, approved_by, tokens_delta_ins/del). |
| `/workspace/files/approve` | POST | Stage whole file: baseline = current content. |
| `/workspace/files/reject` | POST | Revert to baseline (or delete if never approved). |
| `/workspace/files/approve-hunks` | POST | Partial stage by hunk index OR 12-char hash. |
| `/workspace/files/reject-hunks` | POST | Partial revert by hunk index OR hash. |
| `/workspace/commit` | POST | `git add` + `git commit` over approved files. |
| `/workspace/git-status` | POST | Refresh git_status flags on every tracked file. |

### Hunk identity is stable across races

Each hunk in a unified diff carries a 12-character SHA-256 id
computed from the hunk header and body. Clients can approve by
hash instead of index to survive concurrent agent writes that
would shift indices. The `approve-hunks` endpoint applies hunks
in **reverse position order** so earlier indices aren't
perturbed by later length changes.

### Baseline persistence

Baselines and history persist to disk under the workspace's
`.digitorn/sessions/{sid}/baselines/` directory:

- `baselines/{path}` - the last-approved snapshot.
- `baselines/{path}.history/rev-NNNN` - prior revisions (FIFO).
- `baselines/{path}.history/_index.json` - index.

The state survives daemon restart.

## Background Sessions

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/apps/{id}/background-sessions` | GET | List bg sessions |
| `/api/apps/{id}/background-sessions` | POST | Create bg session |
| `/api/apps/{id}/background-sessions/{bid}` | GET | Get bg session |
| `/api/apps/{id}/background-sessions/{bid}` | DELETE | Delete |
| `/api/apps/{id}/background-sessions/{bid}/pause` | POST | Pause |
| `/api/apps/{id}/background-sessions/{bid}/resume` | POST | Resume |
| `/api/apps/{id}/background-sessions/{bid}/payload` | GET/PUT/DELETE | Manage payload |
| `/api/apps/{id}/background-sessions/{bid}/payload/files` | POST | Upload payload file |
| `/api/apps/{id}/background-sessions/{bid}/payload/files/{filename}` | DELETE | Remove payload file |

## Triggers

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/apps/{id}/triggers` | GET | List triggers |
| `/api/apps/{id}/triggers/{tid}/fire` | POST | Fire trigger manually |
| `/api/apps/{id}/triggers/{tid}/test` | POST | Test trigger |

## Secrets & Credentials

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/apps/{id}/secrets` | GET | List secrets |
| `/api/apps/{id}/secrets/{key}` | GET | Get secret |
| `/api/apps/{id}/secrets/{key}` | PUT | Set secret |
| `/api/apps/{id}/secrets` | PUT | Set many (with reload) |
| `/api/apps/{id}/secrets/{key}` | DELETE | Delete secret |
| `/api/apps/{id}/required-secrets` | GET | Missing secrets list |
| `/api/apps/{id}/credentials/schema` | GET | Credential schema for app |
| `/api/credentials` | GET | List user credentials |
| `/api/credentials/providers` | GET | Available credential providers |
| `/api/credentials/{cid}` | GET | Get credential |
| `/api/credentials/{cid}` | PUT | Update credential |
| `/api/credentials/{cid}` | DELETE | Delete credential |
| `/api/credentials/{cid}/grant` | POST | Grant credential |
| `/api/credentials/{cid}/grants` | GET | List grants |
| `/api/credentials/{cid}/grants/{gid}` | DELETE | Revoke grant |

## Approvals & Quotas

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/apps/{id}/approvals` | GET | Pending approvals |
| `/api/apps/{id}/approve` | POST | Approve action |
| `/api/apps/{id}/quota` | GET/PUT/DELETE | App quota |
| `/api/apps/{id}/quota/user/{uid}` | GET/PUT/DELETE | User quota |

## Preview Server

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/apps/{id}/preview/{path:path}` | GET | Serve static dist or dev server |
| `/api/apps/{id}/preview-server/status` | GET | Preview server state |
| `/api/apps/{id}/preview-server/logs` | GET | Preview server logs (dev mode) |
| `/api/apps/{id}/preview-server/restart` | POST | Restart dev server |

## Widgets

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/apps/{id}/widgets` | GET | List widgets |
| `/api/apps/{id}/widgets/data/{binding}` | GET | Widget data |
| `/api/apps/{id}/widgets/data/{binding}/stream` | GET | Widget data SSE (legacy) |
| `/api/apps/{id}/widgets/validate` | GET | Validate widget YAML |
| `/api/apps/{id}/widgets/action` | POST | Execute widget action |
| `/api/apps/{id}/widgets/upload` | POST | Upload widget file |
| `/api/apps/{id}/widgets/upload/{user_id}/{sid}/{file_id}/{filename}` | GET | Download uploaded widget file |

## Monitoring

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/apps/{id}/activations` | GET | List activations |
| `/api/apps/{id}/activations/stats` | GET | Activation stats |
| `/api/apps/{id}/activations/{aid}` | GET | Activation details |
| `/api/apps/{id}/activations/{aid}/events` | GET | Per-activation event log |
| `/api/apps/{id}/activations/{aid}/artifacts` | GET | Per-activation artifacts |
| `/api/apps/{id}/notifications/active` | GET | Active notifications |
| `/api/apps/{id}/notifications` | POST | Send notification |
| `/api/apps/{id}/channels/health` | GET | Channel health |
| `/api/apps/{id}/diagnostics` | GET | App diagnostics |
| `/api/apps/{id}/errors` | GET | Error log |
| `/api/apps/{id}/index` | GET | App index |
| `/api/apps/{id}/artifacts/{eid}/download` | GET/HEAD | Artifact download |
| `/api/apps/{id}/payload-schema` | GET | Payload schema for background-session inputs |
| `/api/apps/{id}/interact` | POST | One-shot interaction (widgets) |

## Packages

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/packages` | GET | List installed packages |
| `/api/packages/install` | POST | Install package |
| `/api/packages/{pid}` | GET | Package details |
| `/api/packages/{pid}/check-update` | GET | Check for updates |
| `/api/packages/{pid}/upgrade` | POST | Upgrade package |
| `/api/packages/{pid}/uninstall` | POST | Uninstall |
| `/api/packages/{pid}/icon` | GET | Package icon |
| `/api/packages/{pid}/assets/{path}` | GET | Package asset |

## MCP Servers

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/mcp/catalog` | GET | Public MCP catalog |
| `/api/mcp/catalog/{sid}` | GET | Catalog entry |
| `/api/mcp/search` | GET | Search catalog |
| `/api/mcp/servers` | GET/POST | List/add MCP servers |
| `/api/mcp/servers/{sid}` | GET/DELETE | Get/remove |
| `/api/mcp/servers/{sid}/test` | POST | Test connection |
| `/api/mcp/servers/{sid}/config` | PUT | Update config |
| `/api/mcp/pool` | GET | Pool status |
| `/api/mcp/pool/{sid}/connect` | POST | Connect pool entry |
| `/api/mcp/pool/{sid}/disconnect` | POST | Disconnect pool entry |
| `/api/mcp/pool/health` | GET | Pool health |

## Assets & App OAuth

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/apps/{id}/assets/{path}` | GET | App asset |
| `/api/apps/{id}/icon` | GET | App icon |
| `/api/apps/{id}/files` | GET | List app files |
| `/api/apps/{id}/oauth/authorize` | GET | OAuth start (app-owned flow) |
| `/api/apps/{id}/oauth/callback` | GET | OAuth finish (app-owned flow) |
| `/api/apps/{id}/mcp/pending-oauth` | GET | Pending MCP OAuth |
| `/api/apps/{id}/mcp/{sid}/oauth-token` | POST/DELETE | Inject/revoke MCP OAuth token |

## Users (`/api/users/me/*`)

All per-user routes live under `/api/users/me/`. The daemon derives the user id from the JWT - no explicit user id in the path.

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/users/me/inbox` | GET | User inbox |
| `/api/users/me/inbox/unread_count` | GET | Unread count |
| `/api/users/me/inbox/{iid}/read` | POST | Mark item read |
| `/api/users/me/inbox/read_all` | POST | Mark all read |
| `/api/users/me/inbox/{iid}` | DELETE | Archive item |
| `/api/users/me/sessions` | GET | Sessions across all apps |
| `/api/users/me/approvals` | GET | Pending approvals |
| `/api/users/me/devices` | GET/POST | Registered devices |
| `/api/users/me/devices/{did}` | DELETE | Unregister device |
| `/api/users/me/notification-prefs` | GET/PUT | Notification preferences |
| `/api/users/me/usage` | GET | Usage stats |
| `/api/users/me/quotas` | GET | List the current user's quotas |
| `/api/users/me/profile` | GET/PUT | User profile |
| `/api/users/me/password` | POST | Change password |
| `/api/users/me/avatar` | POST | Upload avatar |
| `/api/users/me/avatar/{filename}` | GET | Fetch avatar |

### Per-user credentials

Credentials are stored per-user (not per-app) and granted to apps explicitly.

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/users/me/credentials` | GET | List user credentials |
| `/api/users/me/credentials/{app_id}/{provider}` | GET/PUT/DELETE | Manage credential |
| `/api/users/me/credentials/{app_id}/{provider}/oauth/start` | POST | Start OAuth flow |
| `/api/users/me/credentials/{app_id}/{provider}/oauth/status` | GET | OAuth status |
| `/api/users/me/credentials/{app_id}/{provider}/oauth/refresh` | POST | Refresh OAuth token |
| `/api/users/me/credentials/{app_id}/{provider}/mcp/start` | POST | Start MCP for this credential |
| `/api/users/me/credentials/{app_id}/{provider}/mcp/stop` | POST | Stop MCP |
| `/api/users/me/credentials/{app_id}/{provider}/mcp/status` | GET | MCP status |
| `/api/oauth/callback` | GET | OAuth callback (receives provider redirect) |

### Credential sharing

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/credentials` | GET/POST | List / create own credentials |
| `/api/credentials/{cid}` | GET/PUT/DELETE | Manage credential |
| `/api/credentials/{cid}/grant` | POST | Grant to an app (singular form) |
| `/api/credentials/{cid}/grants` | GET/POST | List / add grants |
| `/api/credentials/{cid}/grants/{app_id}` | DELETE | Revoke grant |
| `/api/credentials-grants` | GET | All grants for current user |
| `/api/credentials/providers` | GET | Available credential provider schemas |

## Tools (per-app)

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/apps/{id}/tools/search` | GET | Semantic tool search |
| `/api/apps/{id}/tools/categories` | GET | List categories |
| `/api/apps/{id}/tools/categories/{cat}` | GET | Browse category |
| `/api/apps/{id}/tools/{tool:path}` | GET | Full schema |
| `/api/apps/{id}/tools/{tool:path}/execute` | POST | Execute tool |

## Watchers & Background Tasks

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/apps/{id}/watchers` | GET/POST | List / create watcher |
| `/api/apps/{id}/watchers/{wid}` | GET/DELETE | Get / stop watcher |
| `/api/apps/{id}/watchers/{wid}/pause` | POST | Pause |
| `/api/apps/{id}/watchers/{wid}/resume` | POST | Resume |
| `/api/apps/{id}/background-tasks` | GET/POST | List / launch task |
| `/api/apps/{id}/background-tasks/{tid}` | GET/DELETE | Get / cancel |
| `/api/apps/{id}/background-tasks/{tid}/wait` | POST | Block until task completes |

## Modules (runtime introspection)

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/modules` | GET | List loaded modules with manifests. |
| `/api/modules/{mid}` | GET | Module detail. |
| `/api/modules/{mid}/health` | GET | Module health check. |

## Health

| Route | Method | Purpose |
|-------|--------|---------|
| `/health` | GET | Basic liveness. |
| `/healthz` | GET | Kubernetes-style liveness. |
| `/readyz` | GET | Kubernetes-style readiness. |

## Admin endpoints

A separate set of admin-only endpoints exists for system-wide
credential management, runtime configuration, profile
administration, dependency installation, metrics scraping, and
the internal builder UI. They require `is_admin: true` on the
JWT and are not documented publicly. Reach out to your daemon
administrator for the operational reference if you have admin
access.

## Removed Endpoints

- `/chat`, `/chat/stream` - removed. Use `/api/apps/{id}/sessions/{sid}/messages` instead.
- `/sessions/{sid}/events` (SSE) - removed. Use Socket.IO `/events` namespace.
