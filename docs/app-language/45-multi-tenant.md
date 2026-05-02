---
id: multi-tenant
---

# Multi-Tenant App Installs

The same `app_id` can be installed in two parallel scopes:

| Scope | Owner | Who can use it | Who can manage it |
|-------|-------|----------------|--------------------|
| `system` (default) | None (`owner_user_id = null`) | Every user on the daemon. | Admins only. |
| `user` | The installing user (`owner_user_id = <user_id>`) | That user only (and admins). | The owner (and admins). |

Identity is the **composite triple** `(app_id, scope,
owner_user_id)`. The same `app_id` can have one system install
and any number of per-user installs side by side; deploy /
delete / disable / enable operations target a specific scope.

Every behaviour and field on this page maps to real code; entries
are cited with file + line.

## Source of truth

`packages/digitorn/core/api/apps_install.py:182`. The deploy
endpoint sets `owner_user_id = user_id if scope == "user" else
None`. This is the single line that splits the install into one
of the two scope worlds; all downstream lookups,
permissions, and isolation guarantees follow from it.

## Installing for yourself (`scope=user`)

```http
POST /api/apps/install
Authorization: Bearer <jwt>
Content-Type: application/json

{
  "source_type": "yaml",
  "source_uri": "https://...",
  "scope": "user",
  "accept_permissions": true
}
```

The deploy endpoint reads the JWT to determine `user_id` and
stores `owner_user_id = user_id`. From that moment:

- The app is visible only to that user (`GET /api/apps` filters
  by JWT identity).
- Other users hitting `POST /api/apps/<app_id>/run` get a 404 —
  not even a "permission denied", because the lookup misses.
- Admins still see and can manage every install regardless of
  scope.

## Installing as admin (`scope=system`)

```http
POST /api/apps/install
{
  "source_type": "yaml",
  "source_uri": "https://...",
  "scope": "system",
  "accept_permissions": true
}
```

`scope=system` requires admin permissions. Non-admins get a 403
with the explicit message
(`apps_install.py:170-176`):

> Only admins can install apps at `scope='system'`. Use
> `scope='user'` to install for yourself only.

System installs are visible to every user and the same instance
serves all of them — perfect for shared utilities (a chatbot, a
codebase explorer, a status dashboard).

## Coexistence

The `(app_id, scope, owner_user_id)` triple lets the same
`app_id` exist in any number of forms simultaneously:

| Install | scope | owner_user_id |
|---------|-------|----------------|
| 1 | `system` | `null` (admin install) |
| 2 | `user` | `alice@example.com` |
| 3 | `user` | `bob@example.com` |
| 4 | `user` | `carol@example.com` |

When Alice calls `POST /api/apps/<app_id>/run` with her JWT, the
runtime resolves to install #2 (her user-scoped). When an
unauthenticated client hits the same path, it gets the system
install #1. This is the routing behaviour Digitorn relies on for
multi-tenant SaaS deployments.

## Lifecycle ops are scope-aware

`apps_install.py:267-409`. Every install/upgrade/uninstall
operation reads the existing entry's scope + owner first
(`pkg_scope`, `pkg_owner`) and re-asserts it on the next step:

- **Upgrade** — preserves the existing scope and owner so a
  user-scoped install stays user-scoped after upgrade.
- **Uninstall** — operates on the matching `(scope,
  owner_user_id)` row. Uninstalling Alice's user-scoped instance
  doesn't affect the system instance or anyone else's.
- **Enable / Disable** — same; the toggle hits one specific row.

The `enabled` field on the install row controls whether the app
is currently routable. Disabled installs are kept in the
database but skip every dispatch.

## Per-session isolation builds on top

Multi-tenant install scoping fixes one layer (whose row is
served). Two more layers fix the rest:

- **Per-user secrets vault** — credentials with
  `scope: per_user` (in `tools.modules.<id>.credential` or
  `agents[].brain.credential`) are stored encrypted per
  `(user_id, app_id)`. See [credentials.md](../credentials.md).
- **Per-session memory** — the memory module keys by the
  compound `user_id::session_id` tuple (`memory/module.py:187`)
  so two concurrent sessions, even of the same user, never see
  each other's todos / facts / episodes. Verified at
  [Cognitive Memory → Session isolation](05-memory.md#session-isolation).

The three layers compose: a system-scoped app, hosting per-user
sessions, with per-user credentials, never leaks state across
users.

## CLI

```bash
# List all installed apps (current user's view)
digitorn app list

# Deploy a YAML you authored — defaults to user scope when run
# without admin
digitorn app deploy my-app.yaml

# Admin: install at system scope (requires admin token)
digitorn app deploy my-app.yaml --scope system

# Tear down (matches by current user's scope by default)
digitorn app undeploy my-app
```

The CLI threads the JWT from `~/.digitorn/credentials.json` so
`digitorn app deploy` Just Works at the right scope based on
your role.

## Common patterns

| Goal | Pattern |
|------|---------|
| One bot for the whole company. | `scope=system`, admin install. |
| Each user gets their own private build of the same template. | `scope=user`, each user installs the same source URL once. |
| Shared chatbot with per-user OAuth tokens (Slack, Notion). | `scope=system` + `credential.scope: per_user` on the relevant module. The instance is shared; the credentials are private. |
| Public template that users can fork and modify. | Distribute the YAML; each user runs `digitorn app deploy --scope user` to get their own instance. |
| Migrate from per-user → system. | Admin uninstalls every user-scoped row, then re-deploys at `scope=system`. There's no automatic migration — the rows are independent. |

## Compile-time + runtime checks

| Check | Where | Effect |
|-------|-------|--------|
| Admin requirement for `scope=system` | `apps_install.py:170` | 403 with explicit message. |
| App-id uniqueness within a scope | `PackageIdCollision` (`apps_install.py:207`) | 409 with `existing` info. |
| Permissions acceptance | `PermissionsRequired` (`apps_install.py:194`) | 409 with the permission list; client must retry with `accept_permissions: true`. |
| Routing isolation | App lookup at every request reads the JWT's `user_id` and matches the (app_id, scope, owner_user_id) tuple. | Wrong owner → 404 (not 403, by design — masks the existence). |

## Cross-references

- Auth (where `user_id` comes from): [Auth](22-auth.md)
- Per-user credentials (different from per-user installs):
  [credentials.md](../credentials.md)
- Per-session memory isolation:
  [Cognitive Memory → Session isolation](05-memory.md#session-isolation)
- Background sessions (per-user vs broadcast routing):
  [Background Sessions](38-background-sessions.md)
- Triggers + routing keys:
  [Triggers → Routing](09-triggers.md#routing--who-receives-the-activation)
