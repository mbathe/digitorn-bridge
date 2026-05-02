---
id: production
title: Production Deployment
sidebar_position: 36
---

# Production Deployment

What changes when you take a Digitorn daemon out of `digitorn
start` localhost mode and put it on the open internet. Every
control on this page maps to a real flag, env var, or config
field; entries are cited with file + line.

## TLS (HTTPS)

The daemon supports native TLS without a reverse proxy via the
`--tls-cert` / `--tls-key` CLI flags (`server.py:1981-1985`).

```bash
digitorn start \
  --host 0.0.0.0 \
  --tls-cert /etc/ssl/certs/server.pem \
  --tls-key  /etc/ssl/private/server.key
```

Internally the values are passed straight to Uvicorn as
`ssl_certfile` / `ssl_keyfile`. Both flags must be set together;
one without the other is a hard error
(`server.py:2042-2046`).

### Key file permissions warning

If the TLS key file is readable by group or others, the daemon
prints a warning at startup (`server.py:2056-2061`):

```
WARNING: TLS key '/etc/ssl/private/server.key' is readable by group/others
(mode 0o644). Consider: chmod 600 /etc/ssl/private/server.key
```

It checks `stat().st_mode & 0o044` — both group-read and other-read
bits trip the warning.

### Auth without TLS warning

Auth-on + non-localhost host + no TLS prints a yellow warning
(`server.py:2065-2068`) — JWTs travel in plaintext otherwise.
Either add TLS or front the daemon with a TLS-terminating reverse
proxy (nginx, Caddy, Cloudflare).

## Refusal to bind unauthenticated to non-localhost

`server.py:1361-1372`. With `server.auth_enabled: false` and a
non-loopback host, the daemon refuses to start:

```
RuntimeError: Refusing to bind to 0.0.0.0 without authentication.
Set server.auth_enabled=true, bind to 127.0.0.1, or set
server.insecure=true to override.
```

`server.insecure` isn't declared on `ServerConfig` — it's read
via `getattr(settings.server, "insecure", False)`. Set it via
env var: `DIGITORN_SERVER__INSECURE=true`. Doing this on the open
internet is exactly what the message warns about; don't.

## OpenAPI docs

`server.expose_docs: bool` (`config.py:55`, default `False`).
Controls whether `/docs`, `/redoc`, and `/openapi.json` are
mounted (`server.py:1092-1103`). When `auth_enabled: false`, docs
are auto-exposed regardless (dev-mode default). In production,
keep both flags at their defaults.

```yaml
# ~/.digitorn/config.yaml
server:
  expose_docs: false   # default; keep this in prod
```

## CORS

`server.cors_origins` (`config.py:93`) ships with a list of
`https://app.digitorn.ai`, `https://api.digitorn.ai`, and a
handful of localhost ports. The list goes straight into FastAPI's
CORS middleware (`server.py:1107-1109`).

The validator at `config.py:108-116` **rejects** the wildcard
`"*"`:

```yaml
server:
  cors_origins:
    - "https://your-frontend.example.com"
    # - "*"   # ← raises ValueError("Wildcard '*' CORS origin is not allowed")
```

Override on a loopback bind, the daemon swaps `cors_origins` to
`"*"` for Socket.IO so dev clients on random ports work
(`server.py:140-143`). On a non-loopback host, the explicit
allow-list is enforced.

## Rate limiting

`server.rate_limit_rpm` (`config.py:32`, default **100 000**) is
the per-bucket request budget per minute. The default is
intentionally a soft cap — sustained throughput protection is the
job of the buckets below, not this number.

Buckets created in `server.py:1259-1281`:

| Bucket | Quota | Key |
|--------|-------|-----|
| Messages / Run | `rate_limit_rpm` (100 000 default) | Per `app_id` extracted from the URL. |
| `/auth/login`, `/auth/register` | `rate_limit_rpm` | Fixed `__auth__` key. |
| `/api/mcp/*` | `rate_limit_rpm // 2` | Fixed `__admin_mcp__`. |
| `/api/modules/*` | `rate_limit_rpm // 2` | Fixed `__admin_modules__`. |
| `/api/apps/deploy`, `/api/apps/deploy/upload` | `rate_limit_rpm // 2` | Fixed `__admin_deploy__`. |
| Everything else | None | No bucket — there is **no catch-all** (removed in `server.py:1312-1317` because legitimate Flutter polling kept hitting 429). |

When a bucket trips, the daemon returns `429` with `Retry-After`
and `retry_after` in the JSON body (`server.py:1323-1333`).

To tighten or loosen for production, set
`server.rate_limit_rpm` (config + env). For per-app overrides:

```http
PUT /api/apps/{app_id}/quota
Authorization: Bearer <admin-jwt>
{"rpm": 200}
```

## SSRF protection

Outbound HTTP requests from the `web` / `http` modules pass
through `validate_url()` at `modules/http/security.py`. The
private-network blocklist (`security.py:41-58`) covers:

- Loopback: `0.0.0.0/8`, `127.0.0.0/8`, `::1/128`
- RFC 1918: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`
- Carrier-grade NAT: `100.64.0.0/10`
- AWS / GCP metadata endpoint: `169.254.0.0/16` (covers the
  `169.254.169.254` magic IP)
- IPv6 link-local: `fe80::/10`
- IPv6 ULA: `fc00::/7`
- Multicast / reserved: `224.0.0.0/4`, `240.0.0.0/4`,
  `255.255.255.255/32`
- Benchmarking: `198.18.0.0/15`
- IETF reserved: `192.0.0.0/24`, `ff00::/8`

### DNS rebinding protection

`ValidatedURL` (`security.py:87-100`). The validator resolves the
hostname **once**, replaces it with the resolved IP in
`pinned_url`, and that's the URL the HTTP client connects to.
The original hostname is preserved in the `Host` header for TLS
SNI and vhost routing, but the connection IP is locked.

```
Validation: example.com → 93.184.216.34 (public IP, accepted)
Connection: 93.184.216.34 directly (no re-resolve)
Host header: example.com (for TLS SNI + vhost)
```

This blocks the attack where DNS flips from a public IP at
validation time to a private IP at connection time.

## Sandbox

The OS-level sandbox is enabled by default
(`server.sandbox: True`, `config.py:76-83`). Toggle with the
`--sandbox` / `--no-sandbox` CLI flag (`server.py:1987-1989`).

Full reference (levels, namespaces, MCP per-server permissions,
allow_paths, audit log) lives in [OS-Level Sandbox](35-sandbox.md).
Quick recap of the four levels (`schema.py:1419`):

| Level | Layers | Recommended for |
|-------|--------|-----------------|
| `off` | None | Local dev only. |
| `standard` | Landlock + seccomp + cgroups + hardening (single worker). | Single-tenant production. |
| `strict` | + warm pool + user/PID namespaces + capability drop + MDWE. | Multi-tenant, per-session workspaces. |
| `maximum` | + network namespace + seccomp-notify audit + workspace snapshots. | Compliance / hostile-tenant isolation. |

```yaml
security:
  sandbox:
    level: strict
    pool_size: 4
    pool_max: 8
    allow_paths:
      - /data/models           # read-only
      - ~/shared-data:rw       # read-write
    audit: true                # JSONL trail per session
```

## Serialisation safety

Every backend store (Redis, DiskCache, KV) uses **JSON-only**
serialisation. The CI `security` job at
`.github/workflows/ci.yml:130-177` greps the entire codebase
on every push:

```bash
grep -rn "import pickle\|pickle\.loads\|pickle\.dumps" packages/digitorn/
# zero hits → CI passes
```

Unknown dataclass types degrade to plain dicts — there is no
code-execution path through deserialisation.

## CI security pipeline

`.github/workflows/ci.yml:130-185`. The `security` job runs on
every push and PR to `main`:

| Step | What it checks |
|------|----------------|
| Dependency audit | `pip-audit --strict --desc` against the locked dependency tree (warning, not error). |
| Hardcoded secrets | Greps source for credential-shaped strings. Errors out on hits. |
| Zero pickle | `grep -rn "import pickle\|pickle\.loads\|pickle\.dumps" packages/digitorn/`. Errors on any hit. |
| Safe YAML | Greps for `yaml.load(` (without `safe_`). Errors on any hit. |

Plus the unit-test suite under `tests/security/` (~70 tests) that
verifies sandbox enforcement, path confinement, dangerous-env
blocking, JSON-roundtrip safety, and CORS-wildcard rejection.

## In-process agent calls and auth

The agent runs **inside** the daemon process. App-internal tool
calls (`filesystem.read`, `memory.remember`, MCP, ...) dispatch
directly through Python — no HTTP, no auth check.

When an agent's `http` tool calls **back** to the daemon
(`http://127.0.0.1:8000/api/apps/...`), the request goes through
the normal auth path: `RemoteAuthMiddleware` (from the central
`digitorn_auth` package, registered at `server.py:1397`) requires
a Bearer token. **There is no loopback bypass.** The middleware's
default `allow_paths` covers only `/health`, `/healthz`,
`/.well-known/*`, `/docs`, `/redoc`, `/openapi.json`, `/auth/*`.

If you need an in-process agent to call its own daemon over HTTP,
either pass a real user JWT explicitly (`http.set_credential` →
`api_key` / `bearer_token` handler), or design the tool as a
direct Python module call instead of a HTTP round-trip.

Note: an unused `AuthMiddleware` class in `digitorn_auth/middleware.py`
contains a `_is_loopback_self_call()` helper. It is NOT registered
by the daemon and does not affect production behavior. Earlier
revisions of this page described that helper as live — it is not.

## Production checklist

```text
# --- Transport & Auth ---
[ ] TLS enabled                             (--tls-cert + --tls-key, OR reverse proxy)
[ ] Auth enabled                            (server.auth_enabled: true, default)
[ ] Auth service URL set                    (auth.service_url: https://...)
[ ] CORS origins explicit                   (server.cors_origins: [https://app.example.com])
[ ] expose_docs disabled                    (server.expose_docs: false, default)

# --- Sandbox ---
[ ] Sandbox enabled                         (server.sandbox: true, default)
[ ] Sandbox level set                       (security.sandbox.level: strict|maximum)
[ ] allow_paths reviewed                    (only paths the apps truly need)
[ ] Audit trail on for compliance           (security.sandbox.audit: true)
[ ] MCP per-server permissions declared     (every mcp.servers.<id>.sandbox)

# --- Rate limits ---
[ ] rate_limit_rpm tuned                    (default 100k = effectively off)
[ ] Per-app quotas set if needed            (PUT /api/apps/{id}/quota)

# --- Storage ---
[ ] Postgres for multi-worker               (database.url: postgresql+asyncpg://...)
[ ] Redis for sessions / KV                 (server.kv_backend: redis://...)
[ ] Backup ~/.digitorn/                     (digitorn.db, server.key, jwt.key, credentials master key)

# --- Master key & credentials ---
[ ] DIGITORN_KMS set                        (env|file|aws_kms|gcp_kms|azure_kv|vault; env+file in dev only)
[ ] DIGITORN_MASTER_KEY in a real KMS       (per-row envelope encryption)
[ ] Audit log periodically verified         (POST /api/admin/credentials/audit/verify)

# --- Operations ---
[ ] CI security job passing                 (.github/workflows/ci.yml security)
[ ] Log monitoring for "sandbox_blocked", "denied", "circuit_breaker_open"
[ ] OAuth refresh loop healthy              (GET /api/credentials-health)
```

## Cross-references

- Daemon Settings reference (every server / database / auth /
  sandbox / kv_backend field): [Settings](../configuration.md)
  *(see also `config.py:25-100`)*
- OS-Level Sandbox detail page:
  [OS-Level Sandbox](35-sandbox.md)
- Credentials encryption + KMS modes:
  [credentials.md](../credentials.md)
- Auth model + JWT verification:
  [Auth](22-auth.md)
- Rate-limit + retry semantics in API responses:
  [API Integration → Errors](14-api-integration.md#error-classification)
- Multi-tenant install scopes:
  [Multi-Tenant App Installs](45-multi-tenant.md)
