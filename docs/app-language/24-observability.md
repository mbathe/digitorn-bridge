---
id: observability
title: Observability & Monitoring
---

# Observability & Monitoring

Digitorn ships with built-in health checks, metrics, tracing, and structured logging. No external dependencies are required -- everything works out of the box with the daemon.

## Health Check Endpoints

Three endpoints cover basic liveness, Kubernetes probes, and readiness gates.

| Endpoint | Purpose | Typical consumer |
|------------|----------------------------------------------|--------------------------|
| `GET /health` | Simple health check. Returns `200 OK` with version info. | Load balancers, uptime monitors |
| `GET /healthz` | Kubernetes **liveness** probe. Returns `{"status": "alive"}`. | `livenessProbe` in pod spec |
| `GET /readyz` | Kubernetes **readiness** probe. Reports database, deployed app count, and active request count. Returns `503` during graceful shutdown. | `readinessProbe` in pod spec |

During graceful shutdown the daemon continues to serve health probes while rejecting new work on all other routes (HTTP 503).

### Example: Kubernetes pod spec

```yaml
livenessProbe:
  httpGet:
    path: /healthz
    port: 8340
  periodSeconds: 10
readinessProbe:
  httpGet:
    path: /readyz
    port: 8340
  periodSeconds: 5
```
## Metrics

The `MetricsCollector` class (`packages/digitorn/core/metrics.py`) collects counters, gauges, and histograms in-process. It is thread-safe and has zero external dependencies.

### Python API

```python
from digitorn.core.metrics import metrics

# Counter -- increment by 1 (default) or any delta
metrics.inc("requests_total", app_id="my-app", user_id="u1")

# Histogram -- observe a value (auto-bucketed)
metrics.observe("llm_latency_seconds", 1.23, app_id="my-app")

# Gauge -- set an absolute value
metrics.set_gauge("active_sessions", 42, app_id="my-app")

# Snapshot -- returns a dict of all current values
metrics.snapshot()
```

### JSON Endpoint

```
GET /api/metrics
```

Returns a JSON object containing:

```json
{
  "uptime_seconds": 3621.4,
  "counters": {
    "requests_total": {"app_id=my-app|user_id=u1": 87}
  },
  "gauges": {
    "active_sessions": {"app_id=my-app": 42}
  },
  "histograms": {
    "llm_latency_seconds": {
      "app_id=my-app": {
        "count": 54,
        "avg": 1.1032,
        "p50": 1.0,
        "p95": 2.5,
        "p99": 5.0
      }
    }
  }
}
```

### Prometheus Endpoint

```
GET /api/metrics/prometheus
Content-Type: text/plain; version=0.0.4; charset=utf-8
```

All metrics are prefixed with `digitorn_` and emitted in the standard Prometheus text exposition format. Counters, gauges, and histograms (with bucket boundaries) are all supported. Labels are derived from the scope keys passed at recording time (`app_id`, `user_id`, etc.).

Example output:

```
# TYPE digitorn_requests_total counter
digitorn_requests_total{app_id="my-app",user_id="u1"} 87
# TYPE digitorn_active_sessions gauge
digitorn_active_sessions{app_id="my-app"} 42
# TYPE digitorn_llm_latency_seconds histogram
digitorn_llm_latency_seconds_bucket{app_id="my-app",le="0.25"} 4
digitorn_llm_latency_seconds_bucket{app_id="my-app",le="0.5"} 12
digitorn_llm_latency_seconds_bucket{app_id="my-app",le="1.0"} 31
digitorn_llm_latency_seconds_bucket{app_id="my-app",le="+Inf"} 54
digitorn_llm_latency_seconds_sum{app_id="my-app"} 59.57
digitorn_llm_latency_seconds_count{app_id="my-app"} 54
```

### Default Metrics

The daemon automatically records these metrics during normal operation:

| Metric | Type | Description |
|------|------|-------------|
| `requests_total` | counter | HTTP requests, labelled by `app_id` |
| `llm_calls_total` | counter | LLM API calls |
| `llm_latency_seconds` | histogram | LLM call duration (bucket boundaries: 10ms to 60s) |
| `tool_calls_total` | counter | Tool executions |
| `tool_errors_total` | counter | Tool execution failures |
| `active_sessions` | gauge | Current active session count |
| `compaction_total` | counter | Context compaction events |

## Tracing

Lightweight distributed tracing (`packages/digitorn/core/tracing.py`) propagates `trace_id` and `span_id` through the full request lifecycle:

```
API request → agent_loop → LLM call → tool execution → response
```

No external dependency is required. If OpenTelemetry is installed, traces are compatible with its format.

### Python API

```python
from digitorn.core.tracing import Tracer, current_trace

tracer = Tracer()

with tracer.span("agent_turn", app_id="my-app") as span:
    span.set("turns", 3)
    with tracer.span("llm_call", model="deepseek") as child:
        # child.parent_id == span.span_id
        ...

# Retrieve the current trace context
trace = current_trace()
# → {"trace_id": "a1b2c3d4e5f6", "total_ms": 1842.3, "span_count": 5, "spans": [...]}
```

### Span structure

Each `SpanRecord` captures:

- **name** -- the operation (`agent_turn`, `llm_call`, `tool_exec`, etc.)
- **span_id** -- unique 12-character hex identifier
- **parent_id** -- links to the parent span (or `None` for root spans)
- **duration_ms** -- wall-clock duration
- **attributes** -- arbitrary key-value metadata (model name, app_id, error messages)
- **status** -- `ok` or `error` (set automatically on exception)

Spans nest naturally via Python context managers. Exceptions are caught, recorded on the span, and re-raised.

### Trace context propagation

Traces use `contextvars` so they propagate automatically through async code within the same request. Call `Tracer.start_trace()` at the beginning of a request and `Tracer.end_trace()` at the end to get the complete trace tree.

## Structured Logging

The daemon supports two logging formats, configured in the YAML settings:

```yaml
logging:
  level: info      # debug | info | warning | error | critical
  format: console  # console | json
```
### Console format

Human-readable colored output, suitable for local development:

```
2026-03-18 10:23:45 INFO  [agent_loop] app=my-app turns=3 model=deepseek-chat
```

### JSON format

JSON-lines output, suitable for log aggregation pipelines (ELK, Loki, Datadog):

```json
{"timestamp": "2026-03-18T10:23:45Z", "level": "info", "logger": "agent_loop", "app": "my-app", "turns": 3, "model": "deepseek-chat"}
```

Set `format: json` for production deployments where logs are ingested by a collector.

## Grafana / Dashboard Setup

To scrape Digitorn metrics with Prometheus, add a scrape target pointing at the daemon:

```yaml
# prometheus.yml
scrape_configs:
  - job_name: digitorn
    scrape_interval: 15s
    metrics_path: /api/metrics/prometheus
    static_configs:
      - targets: ["localhost:8340"]
```
From there, build Grafana dashboards using the `digitorn_` prefixed metrics. Useful panels to start with:

- **Request rate**: `rate(digitorn_requests_total[5m])` by `app_id`
- **LLM latency p95**: `histogram_quantile(0.95, rate(digitorn_llm_latency_seconds_bucket[5m]))`
- **Tool error rate**: `rate(digitorn_tool_errors_total[5m]) / rate(digitorn_tool_calls_total[5m])`
- **Active sessions**: `digitorn_active_sessions`

For alerting, a good starting rule is LLM p95 latency exceeding your SLA threshold:

```yaml
# alertmanager rule
- alert: HighLLMLatency
  expr: histogram_quantile(0.95, rate(digitorn_llm_latency_seconds_bucket[5m])) > 5
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "LLM p95 latency above 5s"
```