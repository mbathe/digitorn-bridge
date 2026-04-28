---
id: multi-tenant
---

# Multi-Tenant App Installs

Digitorn lets the **same `app_id`** exist in two parallel states:

- **System scope** (default) - a single global install, visible to every user, managed by admins.
- **User scope** - a private per-user install that only that user (and admins) can see or call.

The two can coexist: Alice, Bob and the system can each hold `my-app` simultaneously without clashing.

## The composite identity

At every layer - DB, disk, memory, API - an install is addressed by a **triple**:

```
(app_id, scope, owner_user_id)
```

| Scope | Owner | Meaning |
|---|---|---|
| `"system"` | `""` (empty) | Global install - visible to every user |
| `"user"` | `"alice"` | Alice's private install |

Uniqueness is enforced at the DB level via a composite unique index on
`(app_id, scope, owner_user_id)` in `applications`. You can't have two system installs of the same `app_id`, and you can't have Alice install `my-app` twice - but a system install, Alice's install and Bob's install all coexist cleanly.

## Where scope lives on disk

| Scope | Bundle dir |
|---|---|
| `system` | `~/.digitorn/apps/{app_id}/` |
| `user` | `~/.digitorn/apps/_@{owner_user_id}__{app_id}/` |

The `_@<uid>__` prefix is an intentionally invalid `app_id` shape (real
app_ids use `[a-z0-9_-]`), so user-scoped bundles can never collide with a
system bundle or another user's. Existing system deploys keep their path unchanged - the scoping refactor is backward-compatible.

## How the daemon picks a scope

Each endpoint resolves the scope from the **caller's JWT**:

| Caller context | Default scope | Can override? |
|---|---|---|
| Non-admin user | `scope=user, owner=<jwt_uid>` if such an install exists, else `scope=system` | Cannot force `scope=system` |
| Admin (perm `*`) | Same default BUT accepts `?scope=system` or `?scope=user&user_id=...` |  ✅ |
| Loopback (in-process agent) | `scope=system` | ✅ (admin-equivalent) |

So a regular user never needs to think about scope - their actions naturally target their own install. Admins use the query params to reach system-wide or impersonate a user.

## Deploying per-scope

### System install (default)

```bash
POST /api/apps/deploy/upload
  file: <app.yaml>
  force: true
  # no scope field → system install
```

### User install (private to the caller)

```bash
POST /api/apps/deploy/upload
  file: <app.yaml>
  force: true
  scope: user      # ← opts into scope=user, owner=<jwt_uid>
```

The daemon inserts a row with `(app_id, "user", <jwt_uid>)`, writes the bundle under `~/.digitorn/apps/_@<jwt_uid>__<app_id>/`, and registers an in-memory `DeployedApp` keyed by `user:<uid>:<app_id>`.

## Deleting per-scope

`DELETE /api/apps/{id}` **targets the caller's scope by default**:

1. If the caller has a user install of `app_id`, that one is deleted.
2. Otherwise, if a system install exists, that one is deleted.
3. Admin can force a specific scope with `?scope=system` or (for impersonation) `?user_id=alice&scope=user`.

**Isolation guarantee** (proven by `TEN02`): when an admin deletes the system install, Alice's and Bob's user installs stay untouched. When Alice deletes her install, the system install survives.

See `DELETE /api/apps/{id}` in the [REST API](../protocol/REST_API.md#apps-deployment) for the full parameter list.

## Disabling per-scope

Same rules: `POST /api/apps/{id}/disable?scope=...` flips `disabled=true` on exactly one `(app_id, scope, owner_user_id)` row. Other scopes of the same app stay live.

### Admin re-enable

Only an admin can re-enable a disabled app:

```bash
POST /api/apps/my-app/enable                    # system install
POST /api/apps/my-app/enable?scope=user&user_id=alice   # Alice's install
```

## Listing with scope

`GET /api/apps` - default view:
- Non-admin: user's own user-scoped deploys + every system-scoped deploy (user shadows system when both exist for the same `app_id`).
- Admin: every deploy, all users, all scopes.

`GET /api/apps?include_disabled=true` - admin-only flag:
- Non-admin: the flag is **silently ignored**. They still only see their own apps.
- Admin: appends every `disabled=true` row across all scopes (DB read).

Response entries always carry `scope` and `owner_user_id`, so the client can render a badge like "System" or "Private (alice)":

```json
{
  "app_id": "my-app",
  "scope": "user",
  "owner_user_id": "alice",
  "disabled": false,
  // ...
}
```

## Lifecycle semantics per scope

| Scope | DELETE default | DELETE `?delete_history=false` | Disable | Enable |
|---|---|---|---|---|
| `user` | Alice's row gone, her bundle gone. Other scopes untouched. | Row kept (`disabled=true`), bundle wiped, her sessions kept. | Row flipped `disabled=true` for Alice only. | Admin flips back + redeploys. |
| `system` | System row gone, system bundle gone. User installs survive. | System row kept (`disabled=true`), system bundle wiped, sessions kept. | All users lose access to the system install. | Admin flips back + redeploys. |

## Worked example

```
State 0 - nothing deployed.

1. Admin deploys my-app system-wide:
   POST /deploy/upload   (no scope)
   → rows: [(my-app, system, "")]

2. Alice installs her own copy:
   POST /deploy/upload   scope=user
   → rows: [(my-app, system, ""), (my-app, user, alice)]

3. Alice disables her install:
   POST /api/apps/my-app/disable
   → Alice's row flipped disabled=true; system still active for everyone else.

4. Admin deletes the system install:
   DELETE /api/apps/my-app?scope=system
   → system row gone. Alice's row still there (disabled).

5. Bob can no longer use my-app (system gone, no user install for Bob).
   Alice still has her install in DB but it's disabled.

6. Admin re-enables Alice's install:
   POST /api/apps/my-app/enable?scope=user&user_id=alice
   → Alice's row flipped disabled=false + redeployed from her bundle.

State 6 - only Alice has access to my-app; everyone else sees 404.
```

## Behavior contract (proved by tests)

- `TEN01` - Two rows for the same `app_id` coexist in DB without collision.
- `TEN02` - `DELETE ?scope=system` does not touch user-scoped rows.
- `TEN03` - Disabled user install is hidden from default listings but visible to admin via `?include_disabled=true`.

All three are in `tools/behavior_tests.py` and pass on the live daemon.

## Security notes

- Non-admins cannot target `scope=system` for delete/disable - they get **403** with a clear message.
- Non-admins cannot enable any disabled app - `/enable` is admin-only regardless of scope.
- The JWT `user_id` is the **only** source of truth for the caller's identity. Loopback calls inside the daemon are treated as admin context (never as a real user's scope).

See [Security](11-security.md) for the broader capabilities model and [REST API](../protocol/REST_API.md) for the exact request/response shapes.
