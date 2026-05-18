---
id: rest
title: HTTP API
---

# HTTP API

The daemon exposes its full surface over HTTP under `/api/`,
plus an authentication surface under `/auth/` and Kubernetes
liveness probes (`(health probe)`, `(liveness probe)`, `(readiness probe)`).

The full route reference - `/auth/*`, `(apps API)`,
`(credentials API)*`, `(user API)*`, `(discovery API)*`,
`(modules API)*`, `(MCP API)*`, plus admin / builder /
metrics / config / requirements surfaces - **is not
documented publicly**.

This is a deliberate operational choice: every endpoint is
authenticated (JWT Bearer with role checks where applicable)
and listing the routes in public docs only helps an attacker
map the surface without giving legitimate clients any
information they don't already have through the official
SDKs.

## What public clients should use instead

For every common task, the public surface is the SDK and the
CLI, not the raw HTTP routes:

| Task | Use this |
|------|----------|
| Build apps, manage sessions, send messages | [Python testing SDK](../client-sdks/python-testing.md) (`DevClient`) |
| Build a Lovable-style preview | [React Preview SDK](../../language/47-preview-sdk.md) |
| Subscribe to live events | [Socket.IO Protocol](socketio.md) |
| Drive everything from a terminal | [CLI reference](../cli/) |
| Authenticate against the daemon | The SDK / CLI handles tokens automatically |

## What you can rely on

The contract every public-facing client depends on is:

1. **Socket.IO `/events` namespace** for live event streaming
   (documented in [Socket.IO Protocol](socketio.md)).
2. **JWT Bearer auth** on every `/api/*` request, with no
   loopback bypass.
3. **The 8-block YAML language** (the
   [language reference](../../language/)) is the stable
   declarative surface.
4. The CLI commands documented in [CLI reference](../cli/).

If you need direct HTTP access for an unusual case (custom
infrastructure, alternative SDK port), reach out to your
daemon administrator for the operational reference.
