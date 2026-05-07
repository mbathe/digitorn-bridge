---
id: auth
title: Authentication (private)
---

# Authentication

The daemon's HTTP authentication surface (`/auth/*`) and the
JWT issuance / refresh / revocation flows are **not
documented publicly**.

Public clients should use the SDK or CLI to obtain and
refresh tokens automatically. Manual token handling is not
recommended.

| Need | Use |
|------|-----|
| Log in from a script | [Python testing SDK](../reference/client-sdks/python-testing.md) - `DevClient` reads the token from `~/.digitorn/auth.token` automatically |
| Log in from the terminal | `digitorn login` (CLI) |
| Pair a daemon with a hosted Digitorn account | `digitorn install-local` (one-time) |

For direct HTTP integration outside of the SDKs, contact your
daemon administrator.

## Per-user installs

`runtime.session_mode: multi` (declared in the
[runtime block](02-app-config.md#runtime---lifecycle-and-execution-policy))
plus the deploy scope (`scope=user` from the JWT, vs.
`scope=system`) is what makes per-user installs work. The
JWT carries the user identity - apps deployed under
`scope=user` are private to the bearer.

See [Multi-Tenant Installs](45-multi-tenant.md) for the
`(app_id, scope, owner_user_id)` semantics.
