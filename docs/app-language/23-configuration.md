---
id: configuration
title: Daemon Configuration
---

# Daemon Configuration

Digitorn's daemon (`digitorn start`) is configured through a layered system of defaults, config files, and environment variables. This reference covers every configurable field.

## Config Loading Priority

Configuration is resolved in order, with later sources overriding earlier ones:

1. **Built-in defaults** - hardcoded in `config.py`
2. **System config** - `/etc/digitorn/config.yaml`
3. **User config** - `~/.digitorn/config.yaml`
4. **Environment variables** - `DIGITORN_` prefix, `__` separator for nesting

Environment variables always win. Nesting uses double underscores:

```bash
# server.port = 9000
export DIGITORN_SERVER__PORT=9000

# database.echo = true
export DIGITORN_DATABASE__ECHO=true
```

---

## Server

Controls the HTTP server and cross-worker infrastructure.

| Field | Type | Default | Range | Description |
| ----- | ---- | ------- | ----- | ----------- |
| `host` | string | `"127.0.0.1"` | - | Bind address |
| `port` | int | `8000` | 1024–65535 | Bind port |
| `workers` | int | `1` | 1–16 | Number of uvicorn worker processes |
| `reload` | bool | `false` | - | Auto-reload on code changes (dev only) |
| `rate_limit_rpm` | int | `60` | 1–100000 | Default requests per minute per app |
| `kv_backend` | string \| null | `null` | - | KV backend URL. `null` = DiskCache, `"redis://host:6379/0"` = Redis |
| `cors_origins` | list[string] | `["http://localhost", "http://127.0.0.1"]` | - | Allowed CORS origins |

```yaml
server:
  host: "0.0.0.0"
  port: 9000
  workers: 4
  reload: false
  rate_limit_rpm: 120
  kv_backend: "redis://redis:6379/0"
  cors_origins:
    - "http://localhost"
    - "http://127.0.0.1"
    - "https://my-frontend.example.com"
```
**Environment variables:**

```bash
export DIGITORN_SERVER__HOST="0.0.0.0"
export DIGITORN_SERVER__PORT=9000
export DIGITORN_SERVER__WORKERS=4
export DIGITORN_SERVER__RATE_LIMIT_RPM=120
export DIGITORN_SERVER__KV_BACKEND="redis://redis:6379/0"
```

### KV Backend Details

The KV backend is shared by sessions, rate limits, and the job store.

| Backend | When to use | Notes |
| ------- | ----------- | ----- |
| **DiskCache** (default) | Single-host, development, small deployments | SQLite-backed, zero-config. Data in `~/.digitorn/state/` |
| **Redis** | Multi-host production | Shared across workers and hosts. Requires a running Redis instance |
| **ResilientRedisBackend** | Production with high availability requirements | Circuit breaker pattern: after 3 consecutive Redis failures, falls back to DiskCache for 30 seconds, then retries Redis. Transparent to consumers |

---

## Database

Controls the application metadata database (deployed apps, session metadata).

| Field | Type | Default | Range | Description |
| ----- | ---- | ------- | ----- | ----------- |
| `url` | string | `"sqlite+aiosqlite:///digitorn.db"` | - | SQLAlchemy async connection URL |
| `echo` | bool | `false` | - | Log all SQL statements (debug) |
| `pool_size` | int | `5` | 1–50 | Connection pool size (ignored for SQLite) |

```yaml
database:
  url: "postgresql+asyncpg://user:pass@db:5432/digitorn"
  echo: false
  pool_size: 10
```
**Environment variables:**

```bash
export DIGITORN_DATABASE__URL="postgresql+asyncpg://user:pass@db:5432/digitorn"
export DIGITORN_DATABASE__ECHO=false
export DIGITORN_DATABASE__POOL_SIZE=10
```

---

## Modules

Controls module discovery and loading.

| Field | Type | Default | Range | Description |
| ----- | ---- | ------- | ----- | ----------- |
| `paths` | list[string] | `["modules"]` | - | Directories to scan for custom modules |
| `enabled` | list[string] | `[]` | - | Explicit allowlist (empty = no filter) |
| `disabled` | list[string] | `[]` | - | Explicit blocklist |
| `load_all` | bool | `true` | - | Load all discovered modules by default |

```yaml
modules:
  paths:
    - "modules"
    - "/opt/digitorn/extra-modules"
  enabled: []
  disabled:
    - "hello"
  load_all: true
```
**Environment variables:**

```bash
export DIGITORN_MODULES__LOAD_ALL=true
```

> **Note:** List fields like `paths`, `enabled`, and `disabled` are easier to set via config file than environment variables.

---

## Runtime

Agent runtime tuning. These control loop guards, timeouts, and context management.

| Field | Type | Default | Range | Description |
| ----- | ---- | ------- | ----- | ----------- |
| `max_consecutive_failures` | int | `2` | 1–20 | Consecutive tool failures before warning the LLM |
| `max_repeat_window` | int | `6` | 2–50 | Sliding window size for duplicate call detection |
| `max_repeats` | int | `2` | 1–10 | Max identical tool calls allowed in window |
| `max_consecutive_same_tool` | int | `3` | 1–20 | Max consecutive calls to the same tool |
| `tool_timeout` | float | `120.0` | 1.0–3600.0 | Per-tool execution timeout in seconds |
| `context_pressure_threshold` | float | `0.75` | 0.1–0.99 | Context usage ratio that triggers compaction |
| `specialist_context_window` | int | `50000` | 4000–2000000 | Default context window for specialist agents |
| `watch_poll_interval` | int | `5` | 1–300 | File watcher poll interval in seconds |

```yaml
runtime:
  max_consecutive_failures: 3
  max_repeat_window: 10
  max_repeats: 3
  max_consecutive_same_tool: 5
  tool_timeout: 300.0
  context_pressure_threshold: 0.80
  specialist_context_window: 100000
  watch_poll_interval: 10
```
**Environment variables:**

```bash
export DIGITORN_RUNTIME__TOOL_TIMEOUT=300.0
export DIGITORN_RUNTIME__CONTEXT_PRESSURE_THRESHOLD=0.80
export DIGITORN_RUNTIME__SPECIALIST_CONTEXT_WINDOW=100000
export DIGITORN_RUNTIME__WATCH_POLL_INTERVAL=10
```

---

## Logging

Controls daemon log output.

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `level` | string | `"info"` | One of: `debug`, `info`, `warning`, `error`, `critical` |
| `format` | string | `"console"` | One of: `json`, `console` |

```yaml
logging:
  level: "debug"
  format: "json"
```
**Environment variables:**

```bash
export DIGITORN_LOGGING__LEVEL=debug
export DIGITORN_LOGGING__FORMAT=json
```

---

## App

Controls default application behavior.

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `yaml_path` | string \| null | `null` | Path to a YAML app to auto-deploy at daemon startup |
| `stop_on_error` | bool | `false` | Stop the daemon if the auto-deployed app fails to load |

```yaml
app:
  yaml_path: "/opt/digitorn/apps/production.yaml"
  stop_on_error: true
```
**Environment variables:**

```bash
export DIGITORN_APP__YAML_PATH="/opt/digitorn/apps/production.yaml"
export DIGITORN_APP__STOP_ON_ERROR=true
```

---

## Complete Example

A full `~/.digitorn/config.yaml` for a multi-worker production setup:

```yaml
server:
  host: "0.0.0.0"
  port: 8000
  workers: 4
  reload: false
  rate_limit_rpm: 120
  kv_backend: "redis://redis:6379/0"
  cors_origins:
    - "https://app.example.com"
    - "http://localhost:3000"

database:
  url: "postgresql+asyncpg://digitorn:secret@db:5432/digitorn"
  echo: false
  pool_size: 10

modules:
  paths:
    - "modules"
  disabled:
    - "hello"
  load_all: true

runtime:
  tool_timeout: 300.0
  context_pressure_threshold: 0.80
  specialist_context_window: 100000
  max_consecutive_failures: 3

logging:
  level: "info"
  format: "json"

app:
  yaml_path: "/opt/digitorn/apps/main.yaml"
  stop_on_error: true
```
A minimal `~/.digitorn/config.yaml` for local development (everything else uses defaults):

```yaml
server:
  reload: true

logging:
  level: "debug"
```