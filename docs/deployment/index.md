---
id: deployment-index
title: Deployment
---

# Deployment

Operational documentation: how to run a Digitorn daemon as a
service in a production environment, how to plan capacity,
how to harden it against the public internet.

## Pages

| Page | What it covers |
|------|----------------|
| [Cloud preview](cloud-preview.md) | How the static-bundle preview surfaces work behind a reverse proxy (nginx, Caddy). |
| [Production deployment (language/36)](../language/36-production.md) | TLS, auth, sandbox, rate limits, SSRF protection, hardening checklist. |
| [Multi-tenant installs (language/45)](../language/45-multi-tenant.md) | The `(app_id, scope, owner_user_id)` triple, system-wide vs per-user installs. |
| [Daemon configuration](../reference/runtime/configuration.md) | The `Settings` model: server, KV, database, CORS, sandbox, KMS. Where each value comes from. |

## Deployment shapes

Three shapes are supported and tested:

### 1. Single-host

A single Uvicorn worker on a single host. The default `digitorn start`.
Handles tens of concurrent sessions on a 4-vCPU box. Suitable for
internal tools, demos, single-team production.

### 2. Multi-worker on a single host

`digitorn start --workers N`. The daemon's process supervisor
(`process_group.py`) puts every child under a Job Object on Windows,
a process group with `PR_SET_PDEATHSIG=SIGKILL` on Linux, and a
process group with `setpgrp` on macOS. When the parent dies, every
child is terminated. No orphans, no stuck builders.

### 3. Distributed

Multiple daemon instances behind a load balancer, sharing a
Postgres backend and a shared filesystem (or object store) for app
bundles. Sessions are sticky to a worker via a session cookie or
JWT claim; live event streams are best handled by routing to the
worker holding the session.

## Hardening checklist

Before exposing a daemon to the public internet, verify all of:

- [ ] TLS terminates at the daemon or at a fronting proxy
      (`--tls-cert` / `--tls-key`, or proxy with HSTS).
- [ ] Auth middleware is registered (it is by default; do not
      remove).
- [ ] No `claude-code` or other dev-only API key shortcuts in
      production YAMLs - use the credentials vault.
- [ ] `security.sandbox.level` is at least `standard` for any app
      that runs untrusted shell.
- [ ] `runtime.max_turns` and `runtime.timeout` are set to bounded
      values for every agent.
- [ ] Rate limits configured under `daemon.rate_limit`.
- [ ] CORS allow-list is restrictive (`daemon.cors.allow_origins`).
- [ ] The credentials master key (`DIGITORN_MASTER_KEY` or KMS) is
      stored outside the application code, in a secret manager, with
      rotation policy.
- [ ] Audit log verification job is scheduled (the audit chain is
      hash-chained; verify with
      `POST /api/admin/credentials/audit/verify`).
- [ ] Backups for the SQL database are running and tested.

For incident response and SLOs, see
[Observability](../language/24-observability.md).
