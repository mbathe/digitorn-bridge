---
id: api-index
title: HTTP API reference
---

# HTTP API reference

The daemon exposes its surface over three protocols, all on the
same TCP port (default `8000`):

| Protocol | Page | Use it for |
|----------|------|-----------|
| REST (`/api/...`) | [REST API](rest.md) | App lifecycle, sessions, messages, workspace, credentials, secrets, MCP, hub, modules. Synchronous request / response. |
| Socket.IO | [Socket.IO](socketio.md) | Live event stream: turn progress, tool calls, tool results, hooks, workspace updates, agent fan-out. |
| DAP | [DAP (Debug Adapter Protocol)](dap.md) | Optional debugger interface for stepping through agent loops. |

Plus the v1 YAML language JSON Schema for IDE integration:

- [`schema-v1.json`](schema-v1.json)

The OpenAPI spec is served by the running daemon under
`/openapi.json` when `expose_docs: true` (development) and is not
shipped with the static documentation.

## Authentication

Every `/api/*` request requires a Bearer token. The token is issued
by the auth flow described in [Auth](../../language/22-auth.md).
The five "always public" paths are:

- `(health probe)`, `(liveness probe)`
- `/.well-known/*`
- `/docs`, `/redoc`, `/openapi.json`
- `/auth/*`

There is **no loopback bypass** in the registered middleware. An
in-process tool that calls `http://127.0.0.1:8000/api/...` MUST
attach a valid JWT, or use the in-process Python module dispatch
(the default for filesystem, memory, MCP, etc.). Comments in older
parts of the codebase that describe a loopback bypass refer to a
class that is no longer wired in.

## Versioning

The REST surface follows a `/api/{resource}/v{N}` style for
versioned resources (notably `(daemon API)`). v1 endpoints are
still served for backward compatibility but new clients should
target the latest version. See [versioning](../../versioning.md).

## Rate limits

Rate limits are configured under `daemon.rate_limit` in
[Configuration](../runtime/configuration.md). The defaults are
generous; the daemon also enforces per-IP and per-user caps to
prevent runaway clients. Exceeding a limit returns
`429 Too Many Requests` with the relevant `Retry-After` header.
