---
id: auth
title: Authentication and Authorization
sidebar_position: 22
---

# Auth

Digitorn delegates JWT issuance and verification to a central
`digitorn-auth` service (`mode: remote`). The daemon does not
sign tokens itself — it only verifies signatures against the
service's RSA public key (fetched via JWKS and cached). OAuth
provider integration and an offline local-device path are layered
on top. Auth is **on by default** for every API endpoint — disable
it explicitly only in dev.

Every behaviour and field on this page maps to real code; entries
are cited with file + line.

## Overview

| Layer | Purpose | Source |
|-------|---------|--------|
| `AuthConfig` | Daemon-side auth settings (mode, token TTLs, lockout, OAuth providers). | `core/config.py:280` |
| **JWT verification middleware** | Runs before every API endpoint. Decodes the bearer token, attaches `request.state.user_id` and permissions. | `RemoteAuthMiddleware` from `digitorn_auth.fastapi` (registered at `server.py:1397`) |
| **Public allow-list** | Small set of paths skipped by the middleware (health probes, OpenAPI, central-auth callbacks). No loopback bypass — all `/api/*` paths require a Bearer token even from `127.0.0.1`. | `RemoteAuthMiddleware._is_allowed` (`packages/auth/src/digitorn_auth/fastapi.py:172`) |
| **Remote auth delegation** | Required when `auth_enabled: true`. The daemon only verifies JWTs signed by a central `digitorn-auth` service (no local signing). | `mode='remote'` in `AuthConfig` (`config.py:322`) |
| **Local device pairing** | Offline auth via paired devices. | `core/auth/local_device.py`, revalidator at `auth/device_revalidator.py` |
| **OAuth providers** | Google / Microsoft / GitHub / etc. for end-user login. | `OAuthConfig` (`config.py:130`) + handlers under `core/credentials/handlers/oauth2*.py` |

## Auth mode — `remote` is required

`AuthConfig.mode` (`config.py:322`).

The daemon delegates token issuance to a central `digitorn-auth`
service and only verifies signatures locally against the central's
RSA public key (fetched via JWKS and cached). The daemon **does not
sign tokens** — it never had access to a private key. The legacy
`embedded` mode is no longer supported and the daemon refuses to
start with it (`server.py:1374-1389`).

```yaml
auth:
  mode: remote                   # only supported value when auth_enabled=true
  service_url: "https://auth.digitorn.ai"
  accept_issuers:                # extra `iss` values besides service_url
    - "https://auth.internal.example.com"
    - "http://127.0.0.1:8001"    # local dev auth service
  enable_local_device: true      # opt-in; loads LocalDeviceAuth
  access_token_ttl: 0            # informational; the central service owns TTLs
  refresh_token_ttl: 0
  max_login_failures: 5
  lockout_window: 900             # 15 min
```

Combined with `enable_local_device: true`, the daemon can
authenticate users **fully offline** via the device pairing
mechanism (see [Local device](#local-device-pairing) below).

## `AuthConfig` reference

`config.py:280` (`extra: forbid` on the parent `Settings`).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `access_token_ttl` | int ≥ 0 | `0` | Access-token lifetime in seconds. `0` = never expires (dev). |
| `refresh_token_ttl` | int ≥ 0 | `0` | Refresh-token lifetime in seconds. `0` = never expires. |
| `max_login_failures` | int [1, 100] | `5` | Lock the account after N failed login attempts. |
| `lockout_window` | int [60, 86400] | `900` | Lockout window in seconds (default 15 min). |
| `approval_timeout` | float [10, 7200] | `3600.0` | Time to wait for user approval before auto-deny (seconds). |
| `mode` | string | `"embedded"` | Schema default `embedded` is rejected at start; you MUST set `remote`. |
| `service_url` | string | `""` | Base URL of the central `digitorn-auth` service. Required when `mode='remote'`. |
| `accept_issuers` | list[string] | `[]` | Additional `iss` claim values the daemon accepts (cluster + edge proxy + dev loopback). |
| `enable_local_device` | bool | `false` | Load `LocalDeviceAuth` secrets at boot and run the device revalidator. Requires the daemon to be paired (`digitorn install-local`). |

The daemon does NOT load or sign with a JWT secret — token issuance
lives in the `digitorn-auth` service. The only env to set is the
service URL and (optionally) the additional accepted issuers:

```bash
# Production
export DIGITORN_AUTH__MODE=remote
export DIGITORN_AUTH__SERVICE_URL=https://auth.example.com
export DIGITORN_AUTH__ENABLE_LOCAL_DEVICE=true   # offline pairing
```

## Disabling auth (dev only)

`server.auth_enabled` (`config.py:51`) gates the entire middleware
chain. When `false`:

```yaml
server:
  auth_enabled: false       # NEVER do this in production
```

- Every endpoint is reachable without a token.
- Swagger / ReDoc / OpenAPI docs are auto-exposed
  (`config.py:55` `expose_docs` is true when auth is off).
- The daemon logs a prominent warning at startup.

This exists for local dev so you can `curl` the API without
threading a token. **Don't ship it.**

## In-process agent calls — no loopback bypass

The agent runs **in the daemon process**. App-internal tool calls
(`filesystem.read`, `memory.remember`, MCP, `workspace.write`, ...)
dispatch directly through Python — no HTTP, no auth check.

When an agent's `http` tool calls **back** to the daemon
(`http.get('http://127.0.0.1:8000/api/...')`), the call leaves a
socket and re-enters via the daemon's HTTP server, where the auth
middleware sees a fresh request with no Authorization header.

`RemoteAuthMiddleware` (from the external `digitorn_auth` package,
registered at `server.py:1397`) does **not** implement a loopback
bypass: every `/api/*` path requires a Bearer token, including
calls coming from `127.0.0.1`. Its public `allow_paths` list covers
only `/health`, `/healthz`, `/.well-known/*`, `/docs`, `/redoc`,
`/openapi.json`, `/auth/login`, `/auth/register`, `/auth/refresh`,
`/auth/oauth/*`, `/auth/revocations`, `/auth/avatars/*`.

If you need an in-process agent to round-trip through the daemon's
own HTTP API, give the `http` tool a real user JWT (via
`http.set_credential` and a `bearer_token` credential handler). The
recommended alternative is to keep app-internal logic in Python
module dispatch and avoid HTTP self-calls altogether.

> A separate `AuthMiddleware` class in `digitorn_auth.middleware`
> contains a `_is_loopback_self_call` helper that does implement
> the path-based bypass — but that class is **never registered** by
> the daemon. Earlier revisions of this page described it as live;
> they were wrong.

## Local device pairing

`core/auth/local_device.py`. For air-gapped deployments
(`enable_local_device: true` + `mode='remote'`), the daemon stores
a paired device secret on disk and uses it to authenticate the
user even when the central auth service is unreachable.

Pair with:

```bash
digitorn install-local
```

Once paired, the daemon's `device_revalidator` background task
periodically re-verifies the device against the central when
connectivity returns (`auth/device_revalidator.py`). Tokens
issued by the local device carry the `iss` of the paired user,
which must be in `accept_issuers`.

## OAuth providers (end-user login)

`OAuthConfig` (`config.py:130`). Each provider sub-section is an
`OAuthProviderConfig` (`config.py:119`) with `client_id` +
`client_secret`. Set both via env to enable a provider:

```bash
DIGITORN_OAUTH__GOOGLE__CLIENT_ID=...
DIGITORN_OAUTH__GOOGLE__CLIENT_SECRET=...
DIGITORN_OAUTH__MICROSOFT__CLIENT_ID=...
DIGITORN_OAUTH__MICROSOFT__CLIENT_SECRET=...
DIGITORN_OAUTH__GITHUB__CLIENT_ID=...
DIGITORN_OAUTH__GITHUB__CLIENT_SECRET=...
```

`public_base_url` (`config.py:143`) is used to build the OAuth
callback URL: `<base>/auth/oauth/<provider>/callback`. Set it to
your daemon's externally-reachable URL (must match what's
registered with the OAuth provider).

OAuth providers also feed the **per-user credentials vault** for
MCP servers — `mcp.connect(server="notion")` triggers the OAuth
flow using the same provider config. See
[credentials.md](../credentials.md) for the vault, refresh loop,
and audit log.

## API key alternative

API key issuance happens at the central `digitorn-auth` service.
The daemon stores key metadata in the `api_keys` table
(`models.py:919`) so the auth service can look up issuing users.
Inbound API-key acceptance (e.g. `X-API-Key` headers) is handled
by the `RemoteAuthMiddleware` from the `digitorn_auth` package —
consult that service's documentation for the exact header name
and transport format. The `Authorization: Bearer <jwt>` flow is
the path the local code paths exercise and verify.

## Account lockout

`config.py:299, 303`. After `max_login_failures` failed attempts
within `lockout_window` seconds, the account is locked. The lock
auto-releases when the window elapses; an admin can release it
sooner via the user-management API.

Defaults: 5 failures within 15 min → 15-min lockout. Tune via env:

```bash
DIGITORN_AUTH__MAX_LOGIN_FAILURES=10
DIGITORN_AUTH__LOCKOUT_WINDOW=300       # 5 min
```

## Permissions and scopes

Every token carries:

- `user_id` — the user's id, attached to `request.state.user_id`.
- `permissions` — list of permission strings; `"*"` is the
  super-admin wildcard.
- `iss` — issuer URL (must be one of `service_url` /
  `accept_issuers` for `mode='remote'`).

The token claims are translated into a `SecurityProfile` for
in-process tool calls (the same one used by the
[security gates](11-security.md)).

## Multi-tenant / per-user installs

Apps deploy under a `(app_id, scope, owner_user_id)` triple
documented in [Multi-Tenant Installs](45-multi-tenant.md). The
deploy endpoint reads the JWT to determine `owner_user_id` for
per-user installs (`scope: user`).

## Common tasks

### Use a token from the CLI

```bash
# After login, the CLI caches the token under ~/.digitorn/auth.json
digitorn dev chat <app_id> -m "test"     # uses the cached token
```

### Inspect what a token claims

```bash
# Decode (without verifying) — for debugging only.
# The token is the middle of the dot-separated triple; pad the
# base64 if its length is not a multiple of 4 (jwt strips =).
TOKEN="<paste-jwt-here>"
PAYLOAD=$(echo "$TOKEN" | cut -d. -f2)
PAD=$(( (4 - ${#PAYLOAD} % 4) % 4 ))
echo "${PAYLOAD}$(printf '=%.0s' $(seq 1 $PAD))" | base64 -d | jq .
```

### Rotate the signing key

Rotation happens at the central `digitorn-auth` service. The
daemon polls the JWKS endpoint and picks up the new public key
automatically; in-flight tokens signed by the previous key remain
valid for their lifetime if the previous key stays in JWKS, or
are rejected immediately if the rotation is forced.

## Cross-references

- Daemon configuration (every auth field):
  [Daemon Configuration](23-configuration.md)
- Production hardening (TLS, CORS, rate limiting):
  [Production Deployment](36-production.md)
- Per-user app installs:
  [Multi-Tenant Installs](45-multi-tenant.md)
- Credentials vault (separate from user auth):
  [credentials.md](../credentials.md)
- Security gates (consume the auth-derived `SecurityProfile`):
  [Security](11-security.md)
