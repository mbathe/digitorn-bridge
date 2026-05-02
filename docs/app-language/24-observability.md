---
id: observability
title: Observability & Monitoring
---

# Observability

The Digitorn daemon exposes health probes, JSON metrics, and (when
`prometheus_client` is installed) Prometheus-formatted metrics over
HTTP. Per-session metrics also live in-process and are queryable
via the API.

Every endpoint and metric on this page maps to real code; entries
are cited with file + line.

## Health endpoints

`packages/digitorn/core/server.py`. The daemon registers three
health surfaces:

| Path | Source | Purpose |
|------|--------|---------|
| `GET /health` | `server.py:1549` | Rich health probe — version, status, system metrics, event-loop lag, watchdog, worker-pool stats. Status flips to `degraded` when loop lag > 500 ms or the turn pool is saturated. |
| `GET /healthz` | `server.py:1662` | Liveness probe (`{"status": "alive"}`). **Public** — exempt from auth middleware. Used by Kubernetes liveness checks. |
| `GET /readyz` | `server.py:1666` | Readiness probe (`{"status": "draining"}` with HTTP 503 when shutting down, otherwise OK). **Currently requires JWT auth** when `server.auth_enabled=true` — for K8s probes either disable auth, run a sidecar that mints a token, or use `/healthz` instead. |

### `GET /health` — typical response

```json
{
  "status": "ok",
  "version": "1.0.0",
  "socketio": true,
  "warming_up": false,
  "system": {
    "cpu_percent": 4.2,
    "memory_mb": 312.7,
    "threads": 24
  },
  "event_loop_lag_ms": 0.31,
  "event_loop_watchdog": { ... },
  "workers": { "turn_pool": { "active_turns": 3, "max_workers": 16 }, ... }
}
```

Pass `?detailed=1` to include slower checks (`open_files`,
`connections` — skipped by default because they're expensive on
Windows; `server.py:1567`).

The status field flips to `"degraded"` automatically
(`server.py:1606-1615`) when:
- Event-loop lag exceeds 500 ms (the daemon is currently stuck on
  a CPU-bound or blocked task), OR
- The turn pool is saturated (active turns ≥ max workers).

Use this in front-of-daemon load balancers to stop sending new
requests while the daemon is overloaded.

### Kubernetes example

```yaml
livenessProbe:
  httpGet:
    path: /healthz
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10

readinessProbe:
  httpGet:
    path: /readyz
    port: 8000
  initialDelaySeconds: 2
  periodSeconds: 5

startupProbe:
  httpGet:
    path: /health
    port: 8000
  failureThreshold: 30
  periodSeconds: 2
```

## Metrics endpoints

`packages/digitorn/core/metrics.py`. The daemon exposes both JSON
and Prometheus-formatted metrics.

| Path | Source | Format |
|------|--------|--------|
| `GET /api/metrics` | `server.py:1638` | JSON — global summary across all apps and sessions. |
| `GET /api/metrics/prometheus` | `server.py:1642` | Prometheus exposition format — same data as `/api/metrics` but in `# HELP` / `# TYPE` form. |
| `GET /metrics` | `server.py:1653` | Mirror of `/api/metrics/prometheus`. Some Prometheus scrapers hard-code `/metrics`. |
| `GET /api/metrics/sessions` | `server.py:1661` | List of every active session with its metrics. |
| `GET /api/metrics/sessions/{session_id}` | `server.py:1666` | Single-session detail. |
| `GET /api/metrics/apps/{app_id}` | `server.py:1674` | Per-app rollup. |

Prometheus support is **opt-in via dependency** — install
`prometheus_client` to enable the Prometheus endpoints. JSON
endpoints work with no extra dependency.

```bash
pip install prometheus_client
```

## Per-session metrics

`packages/digitorn/core/runtime/session_metrics.py`. Every active
session has a `SessionMetrics` instance
(`session_metrics.py:120`) tracking real-time numbers.

### Fields

| Field | Description |
|-------|-------------|
| `app_id`, `session_id`, `agent_id`, `user_id`, `channel`, `model`, `provider` | Identity. |
| `status` | `active` / `idle` / `closed`. |
| `created_at`, `last_active_at` | Unix timestamps. |
| `turn`, `max_turns` | Current turn count and the configured cap. |
| `prompt_tokens`, `completion_tokens`, `total_tokens` | Cumulative token usage reported by the LLM provider. |
| `llm_calls`, `llm_total_ms`, `llm_last_ms` | LLM latency stats. |
| `tool_calls_total`, `tool_calls_success`, `tool_calls_failed` | Tool-call counters. |
| `tool_metrics` | Per-tool breakdown — `dict[str, ToolMetrics]`. Each `ToolMetrics` (`session_metrics.py:92`) tracks `calls`, `successes`, `failures`, `avg_duration_ms`, `last_duration_ms`, `last_error`. |
| `context` | `ContextBreakdown` — system / tools / messages token split for the current turn. |
| `memory_goal`, `memory_facts_count`, `memory_todos_count` | Memory snapshot. |

### Programmatic access

```python
from digitorn.core.runtime.session_metrics import (
    get_session_metrics,        # session_metrics.py:398
    list_active_metrics,        # session_metrics.py:411
    app_summary,                # session_metrics.py:421
    global_summary,             # session_metrics.py:446
)

# Get a single session's metrics object
m = get_session_metrics(app_id="my-app", session_id="abc-123")
print(m.snapshot())

# All active sessions across all apps
for m in list_active_metrics():
    print(m["app_id"], m["session_id"], m["total_tokens"])

# Per-app rollup
print(app_summary("my-app"))

# Daemon-wide
print(global_summary())
```

The same data is reachable over HTTP via `/api/metrics/sessions`,
`/api/metrics/sessions/{id}`, and `/api/metrics/apps/{app_id}`.

## Per-module health

Modules can expose their own health probe via the daemon's modules
API:

```
GET /api/modules/{module_id}/health
```

`packages/digitorn/core/api/modules.py:458`. Each module decides
what "healthy" means (DB connection alive, MCP server reachable,
HTTP backend responding, ...). The CLI front-end is:

```bash
digitorn modules health
digitorn mcp health           # per-MCP-server health
```

The `mcp.health_check` action (`mcp/module.py:1784`) is the
LLM-callable equivalent for MCP servers.

## Channel health

`packages/digitorn/core/app/channels/registry.py:604, 611`. Each
declared channel exposes a health surface:

```
GET /api/apps/{app_id}/channels/health
```

Returns per-channel `ChannelHealth` snapshots
(`channels/base.py:303`; the abstract `health_check()` is at
`channels/base.py:528`). Useful when triggers depend on inbound
channels (a webhook listener that's lost its connection should
flag `degraded`).

## Credentials health

`GET /api/credentials-health`
(`packages/digitorn/core/api/credentials.py:2298`). Returns the
state of the credentials vault: master-key provider, cipher,
audit-log integrity, OAuth registry, refresh loop. Documented in
[credentials.md](../credentials.md).

## Audit log

Every gate decision in the security layer fires an audit event —
see [Security → Audit log](11-security.md#audit-log). The
append-only trail is queryable via the admin route
`GET /admin/audit-log?target_app_id=<id>&event_type=<pattern>`
(`api/user.py:789`). Filters: `event_type` (supports trailing
`*` wildcard), `actor_user_id`, `target_user_id`,
`target_app_id`, `since_ts`/`until_ts` (ISO8601), `success_only`,
`limit`+`offset`. Admin-only — needs `*` or `admin` permission.
Credential-specific audit data has its own endpoint at
`GET /api/admin/credentials/audit` (hash-chained, verify with
`POST /api/admin/credentials/audit/verify`).

## Logging

The daemon uses Python's stdlib `logging` configured by the
runtime — no third-party log framework is mandatory. Log level is
controlled by the `DIGITORN_LOG_LEVEL` env var (or the `logging`
section of the daemon config). For structured JSON output, set
the appropriate handler in your deployment — Digitorn doesn't
force `structlog` on you.

For configuration of log handlers, formats, and per-module
verbosity, see [Daemon Configuration](23-configuration.md).

## Frontend integration

The web client (`digitorn-builder` and the chat UI) consumes
metrics over Socket.IO event streams, not the HTTP metrics
endpoints — Socket.IO is push-based (events fire as turns
complete), HTTP `/api/metrics` is pull-based (you scrape it on a
schedule). Use the right one for the use case:

| Use case | Surface |
|----------|---------|
| Real-time dashboard inside the app | Socket.IO `metrics:*` events |
| Prometheus scrape, Grafana dashboard | `/metrics` (Prometheus format) |
| One-off ops query | `/api/metrics/sessions/{id}` |
| Kubernetes / load-balancer probe | `/healthz` and `/readyz` |

## Cross-references

- Daemon-level config (log level, metrics enable/disable):
  [Daemon Configuration](23-configuration.md)
- Security audit log:
  [Security → Audit log](11-security.md#audit-log)
- Production hardening (TLS, CORS, rate limiting):
  [Production Deployment](36-production.md)
- API surface (REST + Socket.IO):
  [API Integration](14-api-integration.md)
- Per-module health from the LLM (`mcp.health_check`,
  `channels.provider_status`, ...):
  [Built-in Tools](04b-builtin-tools.md)
