# Integration guide

How any service (the daemon, the hub, a future micro-service) consumes
the central auth service in **3 lines**.

## TL;DR

```python
from fastapi import FastAPI
from digitorn_auth.fastapi import RemoteAuthMiddleware

app = FastAPI()
app.add_middleware(RemoteAuthMiddleware, issuer="https://auth.digitorn.ai")
```

That's it. Every request now requires a valid Bearer token issued by
the central auth service. `request.state.user_id`, `user_email`, `roles`,
`permissions`, `claims` are populated for downstream handlers. Paths
matching the `allow_paths` set (default: `/health`, `/.well-known/*`,
`/docs`, `/openapi.json`, `/auth/login`, `/auth/register`,
`/auth/refresh`, `/auth/oauth/*`) bypass auth.

## Three integration shapes

### 1. Drop-in middleware (recommended)

Use when every endpoint should require auth by default.

```python
app.add_middleware(
    RemoteAuthMiddleware,
    issuer="https://auth.digitorn.ai",
    allow_paths=["/health", "/.well-known/*"],   # extra public paths
    accept_issuers=["digitorn"],                  # legacy embedded daemon tokens
)

@app.get("/protected")
def protected(request: Request):
    return {"hello": request.state.user_id}
```

### 2. Per-endpoint dependency (more explicit)

Use when most endpoints are public and auth gates only specific ones.

```python
from fastapi import Depends
from digitorn_auth.fastapi import install_remote_auth, require_user

@app.on_event("startup")
async def _start():
    await install_remote_auth(app, issuer="https://auth.digitorn.ai")

@app.get("/public")
def public():
    return {"hello": "world"}

@app.get("/me")
def me(claims = Depends(require_user)):
    return {"user_id": claims.user_id, "roles": claims.roles}
```

### 3. Manual client (custom flows)

Use when you need verification logic outside FastAPI (queue workers,
cron jobs, custom protocols).

```python
from digitorn_auth.client import RemoteAuthClient, InvalidToken

client = RemoteAuthClient(issuer="https://auth.digitorn.ai")
await client.start()  # warms JWKS cache once

try:
    claims = client.verify(token)  # synchronous, offline after warmup
    do_work(user_id=claims.user_id)
except InvalidToken as e:
    log.warning("rejected: %s", e)
```

## What lands on `request.state`

After `RemoteAuthMiddleware` validates a token successfully:

| Attribute | Type | Notes |
|---|---|---|
| `user_id` | `str` | sub claim |
| `user_email` | `str \| None` | email claim |
| `roles` | `list[str]` | role names (`admin`, `developer`, …) |
| `permissions` | `list[str]` | merged permissions across roles |
| `claims` | `RemoteAuthClaims` | full payload incl. `claims.raw["features"]` |

## Reading account features

The auth service injects an opt-in `features` claim into the JWT when
the user has an `AccountFeatures` row (set via PUT `/auth/admin/...`):

```python
features = request.state.claims.raw.get("features", {})
if features.get("plan_tier") == "enterprise":
    enable_unlimited_quota()
if not features.get("cloud_enabled"):
    return {"error": "Cloud usage requires a Pro plan"}, 402
```

Defaults (no row) match the implicit free plan: `plan_tier=free`,
`cloud_enabled=False`, `self_host_enabled=True`, `max_paired_devices=5`.

## Daemon-specific: offline auth via device pairing

If your service runs **at the user's home** (not in your cloud), pair
it once online so it can authenticate the user offline forever after.

Config:
```yaml
# ~/.digitorn/config.yaml
auth:
  mode: remote
  service_url: https://auth.digitorn.ai
  enable_local_device: true   # opt-in
```

Then user runs once:
```bash
digitorn install-local --auth https://auth.digitorn.ai --label "MacBook"
```

Browser opens → user signs in → CLI captures the access_token → calls
`POST /auth/devices/pair` → stores the encrypted device_token + cached
JWKS in `~/.digitorn/daemon-secrets.enc`.

From that point on, the daemon's lifespan loads
`LocalDeviceAuth.load()` and starts the `revalidate_loop` background
task. The daemon authenticates the user OFFLINE for ~90 days, with
rolling refresh whenever the daemon next reaches the central service.

If the user revokes the device from the dashboard
(`DELETE /auth/devices/{id}`), the daemon's next periodic ping (default
hourly) gets `valid=false` and wipes the local secrets.

## Migration checklist (daemon-by-daemon)

For each existing daemon you want to switch from embedded to remote:

1. Pin `digitorn-auth >= 0.1.0` in your `pyproject.toml` deps
2. Set `auth.mode = "remote"` and `auth.service_url = ...` in config
3. Restart the daemon — it now validates tokens against the central
4. (Optional) `auth.enable_local_device = true` + `digitorn install-local`
   for offline use

The legacy `auth.mode = "embedded"` (default) keeps working unchanged
during the transition. You can flip one daemon at a time.

## Production checklist

- Run the auth service on `https://auth.digitorn.ai` (or your hostname)
- TLS terminated by the CDN / reverse proxy
- `DIGITORN_AUTH_JWT_ALGORITHM=RS256` (default, do not change)
- `DIGITORN_AUTH_JWT_PRIVATE_KEY_PATH` on encrypted volume
- DB on the same Postgres as the daemon (shared `users` table)
- Run `alembic upgrade head` once to create the `paired_devices` table
- Monitor `/health` and `/.well-known/jwks.json` (daemons cache 24h
  but expect them up for the cold-start path)

## What's NOT auto-magic

- **Token revocation**: stateless JWTs expire on their own (15 min).
  For instant revocation, set short TTLs and rely on
  `/auth/refresh` to re-check the user. A revocation list endpoint
  is on the roadmap for high-security flows.
- **Per-app permissions**: roles + permissions are global. App-scoped
  RBAC (e.g. "user X can deploy app Y but not Z") happens at the
  daemon level today. The `AccountFeatures.flags` JSON bag is a
  good place to start carrying app-specific overrides.
- **OAuth provider rotation**: changing Google/Microsoft client IDs
  requires a service restart. Hot reload of OAuth providers isn't
  wired yet.
