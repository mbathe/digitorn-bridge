---
id: production
title: Production Deployment
sidebar_position: 36
---

# Production Deployment

This guide covers everything needed to run Digitorn securely in production.

## TLS (HTTPS)

The daemon supports native TLS without a reverse proxy:

```bash
digitorn start --tls-cert /etc/ssl/certs/server.pem --tls-key /etc/ssl/private/server.key
```

Or via config file:

```yaml
# ~/.digitorn/config.yaml
server:
  tls_cert: /etc/ssl/certs/server.pem
  tls_key: /etc/ssl/private/server.key
```
### Key file permissions

The daemon warns if the TLS key file is readable by group or others:

```
WARNING: TLS key '/etc/ssl/private/server.key' is readable by group/others
(mode 0o644). Consider: chmod 600 /etc/ssl/private/server.key
```

### Auth without TLS

If auth is enabled on a non-localhost address without TLS, the daemon warns
that tokens are sent in plaintext. For production, always use TLS or a
reverse proxy that terminates TLS.

## Authentication

### Binding to network

The daemon **refuses to start** on a non-localhost address without authentication:

```bash
# This will fail:
digitorn start --host 0.0.0.0
# Error: Refusing to bind to 0.0.0.0 without authentication.

# These work:
digitorn start --host 0.0.0.0 --sandbox  # With auth enabled (default)
digitorn start --host 127.0.0.1          # Localhost only
```

To override (not recommended), set `server.insecure: true` in config.

### OpenAPI docs

When auth is enabled (production), `/docs`, `/redoc`, and `/openapi.json`
are **not mounted**. The API schema is not exposed to unauthenticated users.

To force-enable docs in production, set `server.expose_docs: true` in config.

## OS-Level Sandbox

See [OS-Level Sandbox](35-sandbox.md) for the full documentation.

```bash
digitorn start --sandbox --host 0.0.0.0 --tls-cert cert.pem --tls-key key.pem
```

This enables kernel-level isolation for all apps that declare `capabilities:` in their YAML.

### Sandbox Levels

| Level | Layers | Recommended for |
|---|---|---|
| `standard` | Landlock + seccomp + hardening + cgroups | Single-tenant production |
| `strict` | + warm pool + user/PID namespaces + per-session isolation | Multi-tenant, per-session workspaces |
| `maximum` | + network namespace + seccomp-notify audit + workspace snapshots | Maximum security, compliance |

```yaml
execution:
  sandbox:
    level: strict
    pool_size: 4
    allow_paths:
      - /data/models           # read-only access beyond workspace
      - ~/shared-data:rw       # read-write
```
### What the sandbox enforces

- **Filesystem**: Landlock restricts to workspace + declared `allow_paths` only
- **Secrets**: `~/.digitorn/` read-only at kernel level - apps cannot modify server config or keys
- **Temp isolation**: each worker gets its own private tmpdir - `/tmp` is not shared
- **Shell/exec**: seccomp blocks `execve` unless shell module is present
- **Network**: seccomp blocks `socket`/`connect` unless web/http/database module is present
- **Network filtering**: iptables OUTPUT rules enforce `allowed_hosts` with DNS pre-resolution (strict/maximum)
- **MCP servers**: deny-by-default - each server must declare `sandbox:` permissions (3-layer enforcement: compile + runtime + OS)
- **Process**: PID namespace hides host processes, ptrace blocked
- **Memory**: MDWE blocks `mmap(WRITE+EXEC)` (anti-shellcode)
- **Privileges**: all 41 capabilities dropped, `NO_NEW_PRIVS`, `DUMPABLE=0`
- **Audit**: seccomp-notify intercepts syscalls in real-time, append-only JSONL trail per session

Each layer is independent - if one is bypassed, the others still hold. 69 kernel-level enforcement tests verify that all attack vectors are blocked.

### MCP server sandbox (deny-by-default)

MCP servers are fully controlled by the sandbox. A server without a `sandbox:`
block has **no OS-level rights** and its tools are rejected at runtime.

```yaml
modules:
  mcp:
    config:
      servers:
        github:
          command: npx @modelcontextprotocol/server-github
          sandbox:
            permissions: [process.exec, net.http]
            allowed_hosts: [api.github.com]
        local_tools:
          command: python -m my_tools
          sandbox:
            permissions: [process.exec, fs.read]
            paths:
              read: ['{{workspace}}']
```
See [OS-Level Sandbox - MCP Servers](35-sandbox.md#mcp-servers-deny-by-default) for the full reference.

## Rate Limiting

All endpoints are rate-limited:

| Endpoint category | Default limit | Key |
|---|---|---|
| `/api/apps/{id}/chat` | 60 RPM | Per app_id |
| `/api/apps/{id}/run` | 60 RPM | Per app_id |
| `/api/apps/{id}/chat/stream` | 60 RPM | Per app_id |
| `/auth/login`, `/auth/register` | 60 RPM | Fixed key |
| `/api/mcp/*` | 30 RPM | Fixed key |
| `/api/modules/*` | 30 RPM | Fixed key |
| `/api/apps/deploy` | 30 RPM | Fixed key |

Per-user limits are 1/3 of the app quota. Customize per-app via API:

```bash
PUT /api/apps/{app_id}/quota
{"rpm": 200}
```

## Socket.IO Hardening

Socket.IO (streaming) connections have built-in protections:

- **Queue size limit**: 2000 events max buffered per connection
- **Idle timeout**: Connections without activity are closed after 5 minutes
- **Backpressure**: When the queue is full, new events are dropped (not buffered)

## Serialization Security

All backend storage uses **JSON serialization only**. Pickle has been
completely removed from the codebase.

- Redis backend: JSON with type-aware encoding (bytes, sets, dataclasses)
- Cache backend: JSON with safe fallback for unknown types
- Unknown dataclass types degrade to plain dicts (no code execution)
- CI pipeline verifies zero pickle usage on every commit

## SSRF Protection

### IP validation

Outbound HTTP requests are validated against a comprehensive blocklist
of private/reserved IP ranges:

- `127.0.0.0/8` (loopback)
- `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` (RFC 1918)
- `169.254.0.0/16` (AWS/GCP metadata endpoint)
- `fc00::/7`, `fe80::/10` (IPv6 link-local/private)

### DNS rebinding protection

URL validation resolves DNS **once** and pins the IP address for the actual
HTTP request. This prevents time-of-check-time-of-use attacks where an
attacker changes DNS between validation and connection:

```
Validation: example.com → 93.184.216.34 (public IP, safe)
Connection: uses 93.184.216.34 directly (not re-resolved)
```

The original hostname is preserved in the `Host` header for TLS SNI
and virtual host routing.

## CORS

CORS wildcard (`*`) is **explicitly rejected** by the config validator.
Only specific origins are allowed:

```yaml
server:
  cors_origins:
    - "https://app.example.com"
    - "https://admin.example.com"
```
## CI Security Pipeline

The GitHub Actions CI includes a dedicated `security` job:

```yaml
jobs:
  security:
    steps:
      - Dependency audit (pip-audit)
      - Security hardening tests (77 tests)
      - Scan for hardcoded secrets
      - Verify zero pickle usage
      - Verify safe YAML loading
```
This runs on every push and pull request to `main`.

### What the tests verify

| Category | Tests | What they check |
|---|---|---|
| Filesystem deny-by-default | 6 | No workspace = deny, workspace = confine, symlink escape |
| Shell path confinement | 12 | Absolute paths blocked, system paths OK, unrestricted opt-in |
| Shell forbidden patterns | 10 | rm -rf /, fork bomb, curl pipe bash |
| Session security gates | 13 | Forbidden + path check + all dangerous env vars blocked |
| Serialization safety | 8 | JSON roundtrip, malicious payloads blocked |
| Zero pickle | 2 | Full codebase scan |
| Auth safety | 1 | No exception details in error responses |
| CORS safety | 1 | Wildcard rejected |
| YAML safety | 1 | No unsafe yaml.load() |
| SQL injection | 3 | Identifier validation |
| PDF path safety | 1 | relative_to, not startswith |
| Pipeline SSRF | 2 | URL validation + scheme check |
| Profile immutability | 2 | Frozen dataclasses |
| File permissions | 1 | 0o600, O_CREAT+O_EXCL |
| No unsafe execution | 2 | Zero os.system(), no eval() in modules |
| Sandbox enforcement | 9 | Landlock blocks reads/writes, seccomp blocks exec |

## Production Checklist

```text
# --- Transport & Auth ---
[ ] TLS enabled (--tls-cert + --tls-key)
[ ] Auth enabled (default, verify with --host 0.0.0.0)
[ ] CORS origins configured (not wildcard)

# --- OS Sandbox ---
[ ] Sandbox enabled (default, use --no-sandbox to disable)
[ ] Sandbox level set (strict or maximum for multi-tenant)
[ ] allow_paths reviewed (only paths the app truly needs)
[ ] ~/.digitorn/ is read-only (enforced by Landlock - automatic)
[ ] Private tmpdir per worker (automatic - /tmp not shared)
[ ] Network namespace + iptables filtering for allowed_hosts (strict/maximum)

# --- MCP Security ---
[ ] MCP servers: every server has a sandbox: block with explicit permissions
[ ] MCP servers: allowed_hosts restricted to required domains only
[ ] MCP servers: stdio servers declare process.exec, SSE servers declare net.http
[ ] MCP compile-time validation enabled (automatic when capabilities: present)

# --- Application Security ---
[ ] Rate limits reviewed per app
[ ] Secrets stored via API (not in YAML files)
[ ] Audit trail enabled for compliance (sandbox.audit: true)

# --- Infrastructure ---
[ ] Database backed by PostgreSQL (not SQLite) for multi-worker
[ ] Redis backend for sessions/KV if multi-worker
[ ] Backup ~/.digitorn/ (server.key, jwt.key, digitorn.db)
[ ] CI security pipeline enabled
[ ] Log monitoring for "sandbox_blocked" and "denied" events
```
