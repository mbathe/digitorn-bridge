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
`--tls-cert` / `--tls-key` CLI flags (`server.py`).

```bash
digitorn start \
  --host 0.0.0.0 \
  --tls-cert /etc/ssl/certs/server.pem \
  --tls-key  /etc/ssl/private/server.key
```

Internally the values are passed straight to Uvicorn as
`ssl_certfile` / `ssl_keyfile`. Both flags must be set together;
one without the other is a hard error
(`server.py`).

### Key file permissions warning

If the TLS key file is readable by group or others, the daemon
prints a warning at startup (`server.py`):

```
WARNING: TLS key '/etc/ssl/private/server.key' is readable by group/others
(mode 0o644). Consider: chmod 600 /etc/ssl/private/server.key
```

It checks `stat.st_mode & 0o044` - both group-read and other-read
bits trip the warning.

### Auth without TLS warning

Auth-on + non-localhost host + no TLS prints a yellow warning
(`server.py`) - JWTs travel in plaintext otherwise.
Either add TLS or front the daemon with a TLS-terminating reverse
proxy (nginx, Caddy, Cloudflare).

## Refusal to bind unauthenticated to non-localhost

`server.py`. With `server.auth_enabled: false` and a
non-loopback host, the daemon refuses to start:

```
RuntimeError: Refusing to bind to 0.0.0.0 without authentication.
Set server.auth_enabled=true, bind to 127.0.0.1, or set
server.insecure=true to override.
```

`server.insecure` isn't declared on `ServerConfig` - it's read
via `getattr(settings.server, "insecure", False)`. Set it via
env var: `DIGITORN_SERVER__INSECURE=true`. Doing this on the open
internet is exactly what the message warns about; don't.

## OpenAPI docs

`server.expose_docs: bool` (`config.py`, default `False`).
Controls whether `/docs`, `/redoc`, and `/openapi.json` are
mounted (`server.py`). When `auth_enabled: false`, docs
are auto-exposed regardless (dev-mode default). In production,
keep both flags at their defaults.

```yaml
# ~/.digitorn/config.yaml
server:
  expose_docs: false   # default; keep this in prod
```

## CORS

`server.cors_origins` (`config.py`) ships with a list of
`https://app.digitorn.ai`, `https://api.digitorn.ai`, and a
handful of localhost ports. The list goes straight into FastAPI's
CORS middleware (`server.py`).

The validator at `config.py` **rejects** the wildcard
`"*"`:

```yaml
server:
  cors_origins:
    - "https://your-frontend.example.com"
    # - "*"   # ← raises ValueError("Wildcard '*' CORS origin is not allowed")
```

Override on a loopback bind, the daemon swaps `cors_origins` to
`"*"` for Socket.IO so dev clients on random ports work
(`server.py`). On a non-loopback host, the explicit
allow-list is enforced.

## Rate limiting

`server.rate_limit_rpm` (`config.py`, default **100 000**) is
the per-bucket request budget per minute. The default is
intentionally a soft cap - sustained throughput protection is the
job of the buckets below, not this number.

Buckets created in `server.py`:

| Bucket | Quota | Key |
|--------|-------|-----|
| Messages / Run | `rate_limit_rpm` (100 000 default) | Per `app_id` extracted from the URL. |
| Auth surface | `rate_limit_rpm` | Fixed `__auth__` key. |
| MCP surface | `rate_limit_rpm // 2` | Fixed `__admin_mcp__`. |
| Modules surface | `rate_limit_rpm // 2` | Fixed `__admin_modules__`. |
| Deploy surface | `rate_limit_rpm // 2` | Fixed `__admin_deploy__`. |
| Everything else | None | No catch-all (removed because legitimate the chat client polling kept hitting 429). |

When a bucket trips, the daemon returns `429` with `Retry-After`
and `retry_after` in the JSON body (`server.py`).

To tighten or loosen for production, set
`server.rate_limit_rpm` (config + env). Per-app quota
overrides go through the admin API (consult your daemon
administrator).

## SSRF protection

Outbound HTTP requests from the `web` / `http` modules pass
through `validate_url` at `modules/http/security.py`. The
private-network blocklist (`security.py`) covers:

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

`ValidatedURL` (`security.py`). The validator resolves the
hostname **once**, replaces it with the resolved IP in
`pinned_url`, and that's the URL the HTTP client connects to.
The original hostname is preserved in the `Host` header for TLS
SNI and vhost routing, but the connection IP is locked.

| Step | Value |
|-------------|-----------------------------------------------------------------|
| Validation | `example.com` resolved to `93.184.216.34` (public IP, accepted) |
| Connection | `93.184.216.34` directly (no re-resolve) |
| Host header | `example.com` (for TLS SNI + vhost) |

This blocks the attack where DNS flips from a public IP at
validation time to a private IP at connection time.

## Sandbox

The OS-level sandbox is enabled by default
(`server.sandbox: True`, `config.py`). Toggle with the
`--sandbox` / `--no-sandbox` CLI flag (`server.py`).

Full reference (levels, namespaces, MCP per-server permissions,
allow_paths, audit log) lives in [OS-Level Sandbox](35-sandbox.md).
Quick recap of the four levels (`schema.py`):

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
serialisation. The CI `security` job greps the entire codebase
on every push to ensure no `pickle` import or call slips in.

Unknown dataclass types degrade to plain dicts - there is no
code-execution path through deserialisation.

## CI security pipeline

The `security` job runs on every push and PR to `main`:

| Step | What it checks |
|------|----------------|
| Dependency audit | `pip-audit --strict --desc` against the locked dependency tree (warning, not error). |
| Hardcoded secrets | Greps source for credential-shaped strings. Errors out on hits. |
| Zero pickle | Errors on any pickle import or call across the codebase. |
| Safe YAML | Greps for `yaml.load(` (without `safe_`). Errors on any hit. |

Plus the unit-test suite under `tests/security/` (~70 tests) that
verifies sandbox enforcement, path confinement, dangerous-env
blocking, JSON-roundtrip safety, and CORS-wildcard rejection.

## In-process agent calls and auth

The agent runs **inside** the daemon process. App-internal tool
calls (`filesystem.read`, `memory.remember`, MCP, ...) dispatch
directly through Python - no HTTP, no auth check.

When an agent's `http` tool calls **back** to the daemon
over HTTP, the request goes through the normal auth path:
`RemoteAuthMiddleware` (from the central `digitorn_auth`
package) requires a Bearer token. **There is no loopback
bypass.** Only the liveness probes, OpenAPI docs (when
exposed), and the auth surface itself are unauthenticated.

If you need an in-process agent to call its own daemon over HTTP,
either pass a real user JWT explicitly (`http.set_credential` →
`api_key` / `bearer_token` handler), or design the tool as a
direct Python module call instead of a HTTP round-trip.

Note: an unused `AuthMiddleware` class in `digitorn_auth/middleware.py`
contains a `_is_loopback_self_call` helper. It is NOT registered
by the daemon and does not affect production behavior. Earlier
revisions of this page described that helper as live - it is not.

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
[ ] Per-app quotas set if needed            (admin API)

# --- Storage ---
[ ] Postgres for multi-worker               (database.url: postgresql+asyncpg://...)
[ ] Redis for sessions / KV                 (server.kv_backend: redis://...)
[ ] Backup ~/.digitorn/                     (digitorn.db, server.key, jwt.key, credentials master key)

# --- Master key & credentials ---
[ ] DIGITORN_KMS set                        (env|file|aws_kms|gcp_kms|azure_kv|vault; env+file in dev only)
[ ] DIGITORN_MASTER_KEY in a real KMS       (per-row envelope encryption)
[ ] Audit log periodically verified         (admin API)

# --- Operations ---
[ ] CI security job passing                 (.github/workflows/ci.yml security)
[ ] Log monitoring for "sandbox_blocked", "denied", "circuit_breaker_open"
[ ] OAuth refresh loop healthy              (credentials health endpoint)
```

## Watchdog & supervisor

The daemon ships a multi-layer watchdog stack so an iframe / API
client never sees an unattended crash in production.

### Layer 1 - In-process loop watchdog (always on)

`LoopWatchdog` (`runtime/loop_watchdog.py`) runs from `start` and
detects when the asyncio main loop stalls beyond 2s. On stall it dumps
every Python thread's stack to `~/.digitorn/logs/loop-stall.log`
(overwritten on each stall) and bumps a counter surfaced via
`/health.event_loop_watchdog.stalls_total`.

The companion `_stack_watchdog` daemon thread dumps every 30s
unconditionally to `~/.digitorn/logs/stacks.log` for post-mortem.

### Layer 2 - Lightweight liveness probe

Designed to be called every few seconds by container
orchestrators, load balancers, and the supervisor below.
Cheap (no psutil), returns machine-readable status:

```json
{
  "status": "alive",         // "warming" | "alive" | "degraded"
  "boot_phase": "ready",     // "init_db" | "remote_auth_warm" | ... | "ready"
  "warming_up": false,       // app registry still loading
  "uptime_s": 1234.5,        // seconds since lifespan completed
  "loop_lag_ms": 0.3,        // instantaneous loop lag
  "loop_stalls_total": 0     // cumulative stalls (from layer 1)
}
```

The daemon also exposes a richer system-info probe (CPU,
memory, threads, worker-pool stats) and a per-module health
surface for `web_preview`. Exact paths are in the daemon
administrator's operational reference.

### Layer 3 - Boot-phase progress logging

The lifespan wraps long-running phases (`init_db`,
`remote_auth_warm`, `lifecycle.start_all`) in `_slow_phase` which
logs every 10s while the phase is running:

```
boot_phase init_db elapsed_ms=23
boot_phase remote_auth_warm WAITING elapsed_s=10 - still running, hold on
boot_phase remote_auth_warm WAITING elapsed_s=20 - still running, hold on
boot_phase remote_auth_warm elapsed_ms=24500
```

A user watching the daemon stdout sees that it's not hung, just slow
on a cold start (Neon serverless DB wakeup, JWKS first fetch). Avoids
the "panic kill" reflex that produces phantom incidents.

### Layer 4 - Zombie-poll throttle

A client polling a 404-returning path in tight loop (typical of stale
session ids, bad reconnect logic) gets a 429 fast-reject after 30
404s in a 60s sliding window. Prevents the event loop from being
drowned in zombie traffic. Per-(IP, path) bucket, in-memory, resets
on restart.

When the threshold trips, the daemon logs an
`zombie_poll_detected` event with the offending IP and path
and starts returning 429 to subsequent requests in that
window.

### Layer 5 - `digitorn-supervisor` external watchdog

Cross-platform process supervisor that auto-restarts the daemon on
crash. Use it on dev machines, Raspberry Pis, and anywhere systemd /
Docker isn't available.

```bash
digitorn-supervisor --host 127.0.0.1 --port 8000
```

Trigger conditions for restart:

| Trigger | Default | Tunable |
|---|---|---|
| Process exited (any code) | always | n/a |
| Liveness probe unreachable | 5 consecutive failures (50s) | `--max-failures` |
| Liveness probe `degraded` for 60s | 6 consecutive probes | `--max-degraded` |
| `loop_stalls_total` increased by ≥3 | always | n/a |
| Process RSS exceeds cap | 4 GiB | `--memory-cap-mb` |

After 10 restarts in 1 hour the supervisor exits - assumes the daemon
is genuinely broken and won't help to keep cycling. Tunable via
`--max-restarts-per-hour`.

Backoff between restarts: 2s, 5s, 15s, 30s, 60s (capped). A few hours
of healthy uptime decay the counter so future incidents get a fresh
budget.

Each restart writes a JSON incident to
`~/.digitorn/incidents/YYYYMMDD_HHMMSS_<reason>.json` with the
last 200 lines of daemon stdout, the exit code, the pid, and
the last successful liveness payload. Use it for offline triage:

```bash
ls -lat ~/.digitorn/incidents/ | head
cat ~/.digitorn/incidents/20260105_120000_event_loop_stalls_repeated.json | jq .
```

### Layer 6 - systemd / Docker / NSSM (production OS-level)

For real production servers, prefer the OS supervisor:

**Linux (systemd)**:

```ini
[Unit]
Description=Digitorn daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=digitorn
WorkingDirectory=/opt/digitorn
ExecStart=/opt/digitorn/.venv/bin/digitorn start --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
StartLimitIntervalSec=300
StartLimitBurst=10
KillMode=mixed
TimeoutStopSec=60
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

**Docker / Compose**:

```yaml
services:
  digitorn:
    image: digitorn:latest
    restart: always
    ports: ["8000:8000"]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/<liveness-path>"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 90s
```

**Windows (NSSM)**:

```bat
nssm install Digitorn "C:\digitorn\.venv\Scripts\digitorn.exe" start --port 8000
nssm set Digitorn AppRestartDelay 5000
nssm set Digitorn AppExit Default Restart
nssm start Digitorn
```

When you use systemd / Docker / NSSM, the in-process watchdog (layers
1-4) still runs and surfaces metrics; you don't need the
`digitorn-supervisor` wrapper on top.

### Choosing a layer

| Setup | Use |
|---|---|
| Local dev, no orchestrator | `digitorn-supervisor` (layer 5) |
| Single-server prod, no orchestrator | `digitorn-supervisor` or systemd |
| Linux server with systemd | systemd (layer 6) |
| Container | Docker / k8s liveness probes |
| Multi-node cluster | k8s with horizontal autoscaling + liveness probes |

The watchdog layers compose: even with systemd you still get the
in-process loop watchdog, the boot-phase logging, the zombie-poll
throttle, and the rich liveness payload.

## Cross-references

- Daemon Settings reference (every server / database / auth /
  sandbox / kv_backend field): [Settings](../reference/runtime/configuration.md)
  *(see also `config.py`)*
- OS-Level Sandbox detail page:
  [OS-Level Sandbox](35-sandbox.md)
- Credentials encryption + KMS modes:
  [credentials.md](../reference/runtime/credentials.md)
- Auth model + JWT verification:
  [Auth](22-auth.md)
- Rate-limit + retry semantics in API responses:
  [API Integration → Errors](14-api-integration.md)
- Multi-tenant install scopes:
  [Multi-Tenant App Installs](45-multi-tenant.md)
