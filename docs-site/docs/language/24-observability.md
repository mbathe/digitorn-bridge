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

The daemon registers three health surfaces. The exact paths and
request shapes are in the daemon administrator's operational
reference; publicly:

- A **rich health probe** returns version, status, system
  metrics, event-loop lag, watchdog, worker-pool stats. Status
  flips to `degraded` when loop lag > 500 ms or the turn
  pool is saturated.
- A **liveness probe** is exempt from auth middleware and
  returns `{"status": "alive"}`. Suitable for Kubernetes
  liveness probes.
- A **readiness probe** returns 503 + `{"status": "draining"}`
  while shutting down, otherwise OK. Currently requires
  JWT auth when `server.auth_enabled=true`.

The status field flips to `"degraded"` automatically when
event-loop lag exceeds 500 ms or the turn pool saturates
(active turns ≥ max workers). Use this in front-of-daemon
load balancers to stop sending new requests while the daemon
is overloaded.

## Metrics

The daemon exposes both JSON and Prometheus-formatted metrics
through admin-only endpoints (operational reference held by the
daemon administrator).

Prometheus support is **opt-in via dependency** - install
`prometheus_client` to enable the Prometheus exposition.

```bash
pip install prometheus_client
```

## Per-session metrics

Every active session has a `SessionMetrics` instance tracking
real-time numbers.

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
| `tool_metrics` | Per-tool breakdown - `dict[str, ToolMetrics]`. Each `ToolMetrics` (`session_metrics.py`) tracks `calls`, `successes`, `failures`, `avg_duration_ms`, `last_duration_ms`, `last_error`. |
| `context` | `ContextBreakdown` - system / tools / messages token split for the current turn. |
| `memory_goal`, `memory_facts_count`, `memory_todos_count` | Memory snapshot. |

### Programmatic access

```python
from digitorn.core.runtime.session_metrics import (
    get_session_metrics,
    list_active_metrics,
    app_summary,
    global_summary,
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

The same data is reachable through the admin metrics surface
(see your daemon administrator).

## Per-module health

Modules can expose their own health probe through the
daemon's modules API. Each module decides what "healthy"
means (DB connection alive, MCP server reachable, HTTP
backend responding, ...). The CLI front-end is:

```bash
digitorn modules health
digitorn mcp health           # per-MCP-server health
```

The `mcp.health_check` action (`mcp/module.py`) is the
LLM-callable equivalent for MCP servers.

## Channel health

Each declared channel exposes a per-channel `ChannelHealth`
snapshot through the daemon's channels API. Useful when
triggers depend on inbound channels (a webhook listener
that's lost its connection should flag `degraded`).

## Credentials health

The daemon exposes a credentials-vault health endpoint that
returns the state of the master-key provider, cipher,
audit-log integrity, OAuth registry, and refresh loop. The
exact route is admin-only; consult your daemon administrator.
Documented behavior is in
[Credentials](../reference/runtime/credentials.md).

## Audit log

Every gate decision in the security layer fires an audit
event - see [Security → Audit log](11-security.md#audit-log).
The append-only trail is queryable through admin endpoints
with filters on `event_type`, `actor_user_id`,
`target_user_id`, `target_app_id`, time range, success-only,
`limit`+`offset`. Credential-specific audit data is stored
in a hash-chained table; integrity verification is also
admin-only.

## Logging

The daemon uses Python's stdlib `logging` configured by the
runtime - no third-party log framework is mandatory. Log level is
controlled by the `DIGITORN_LOG_LEVEL` env var (or the `logging`
section of the daemon config). For structured JSON output, set
the appropriate handler in your deployment - Digitorn doesn't
force `structlog` on you.

For configuration of log handlers, formats, and per-module
verbosity, see [Daemon Configuration](23-configuration.md).

## Frontend integration

The web client consumes metrics over Socket.IO event streams,
not the HTTP metrics endpoints - Socket.IO is push-based
(events fire as turns complete), the metrics surface is
admin-only and pull-based.

| Use case | Surface |
|----------|---------|
| Real-time dashboard inside the app | Socket.IO `metrics:*` events |
| Prometheus scrape, Grafana dashboard | admin metrics surface |
| One-off ops query | admin metrics surface |
| Kubernetes / load-balancer probe | liveness / readiness probes |

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
