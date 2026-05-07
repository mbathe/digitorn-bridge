---
id: configuration
title: Daemon Configuration
---

# Daemon Configuration

The Digitorn daemon (`digitorn start`) is configured by the
`Settings` Pydantic model in Settings load from layered
sources (priority highest → lowest):

1. **User config**: `~/.digitorn/config.yaml`
2. **System config**: `/etc/digitorn/config.yaml`
3. **Environment variables** with prefix `DIGITORN_` and nested
   delimiter `__` (e.g. `DIGITORN_SERVER__PORT=9000`)
4. **Defaults** baked into the Pydantic models

(YAML beats env. The two YAML files merge with user config on top
of system. The optional `--config <path>` CLI flag adds a third
file with the highest priority of the three YAMLs.) See
`Settings.load` (`config.py`).

Every field on this page maps to a real Pydantic field; entries
are cited with file + line.

## The 19 sub-blocks

`Settings` (`config.py`) groups configuration into 19 nested
sub-models:

| Block | Pydantic class | Source line | Covers |
|-------|----------------|-------------|--------|
| `server` | `ServerConfig` | `config.py` | FastAPI / Uvicorn - host, port, workers, CORS, TLS, sandbox toggle. |
| `database` | `DatabaseConfig` | `config.py` | Storage backend (SQLite / Postgres). |
| `modules` | `ModulesConfig` | `config.py` | Module loading toggles + per-module config. |
| `logging` | `LoggingConfig` | `config.py` | Log level + format. |
| `app` | `AppConfig` | `config.py` | Built-in app behavior (default conversational app). |
| `runtime` | `RuntimeConfig` | `config.py` | Daemon-wide runtime defaults - context window, timeouts. |
| `default_model` | `DefaultModelConfig` | `config.py` | Default LLM provider/model used by built-in apps when none is set. |
| `auth` | `AuthConfig` | `config.py` | JWT / API-key authentication. |
| `oauth` | `OAuthConfig` | `config.py` | OAuth providers (Google, Microsoft, GitHub, ...). |
| `session` | `SessionConfig` | `config.py` | Session lifecycle defaults. |
| `agent_spawn` | `AgentSpawnConfig` | `config.py` | Defaults for the agent_spawn module (max_workers, timeouts). |
| `mcp` | `MCPConfig` | `config.py` | MCP server connection pool defaults. |
| `sandbox` | `SandboxConfig` (daemon-side) | `config.py` | Per-app OS sandbox enforcement. |
| `websocket` | `WebSocketConfig` | `config.py` | Socket.IO / WS server tuning. |
| `discovery` | `DiscoveryConfig` | `config.py` | Tool discovery (semantic + keyword index). |
| `images` | `ImageConfig` | `config.py` | Multimodal image handling (max size, aging policy). |
| `transcribe` | `TranscribeConfig` | `config.py` | Voice transcription (model selection, fallback). |
| `hub` | `HubConfig` | `config.py` | Hub registry integration. |
| `session_queue` | `SessionQueueConfig` | `config.py` | Background-mode activation queue. |

> **Note**. The daemon's `sandbox` sub-block (`config.py`) is
> the OS sandbox toggle and pool size for **all** deployed apps -
> distinct from `security.sandbox` in an app's YAML
> (`schema.py`), which is the per-app sandbox declaration.
> Both work together: the daemon enables sandboxing globally, the
> app declares the level it wants.

## `server:` - FastAPI / Uvicorn

`config.py` `ServerConfig`. Default values are production-safe
unless explicitly relaxed.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | string | `"127.0.0.1"` | Bind address. Set to `"0.0.0.0"` for external access. |
| `port` | int [1024, 65535] | `8000` | Listen port. |
| `workers` | int [1, 16] | `1` | Number of Uvicorn worker processes. |
| `reload` | bool | `false` | Auto-reload on file changes (dev only). |
| `rate_limit_rpm` | int [1, 100_000] | `100_000` | Default per-app requests-per-minute cap. Effectively disabled at the default; specific buckets (auth, admin, deploy) have tighter caps in `server.py`. |
| `kv_backend` | string \| null | `null` (DiskCache) | KV backend URL. `redis://host:6379/0` for multi-host production; `null` falls back to a single-host SQLite-backed `DiskCache`. |
| `auth_enabled` | bool | `true` | JWT / API-key authentication on every API endpoint. **Never disable in production.** |
| `expose_docs` | bool | `false` (true when `auth_enabled=false`) | Expose Swagger / ReDoc / OpenAPI. Off by default in prod (the API surface is an attacker's best friend). |
| `turn_workers` | int [1, 512] | `32` | Thread pool for agent turns. One thread per turn. |
| `io_workers` | int [1, 1024] | `64` | Thread pool for blocking I/O (KV, FS). |
| `sandbox` | bool | `true` | Enable OS-level sandboxing for deployed apps with capabilities. |
| `node_auto_install` | bool | `true` | Auto-download Node.js LTS at boot if not found. Disable for air-gapped / CI. |
| `cors_origins` | list[string] | `[localhost:*, app.digitorn.ai, api.digitorn.ai]` | Allowed CORS origins. **Wildcard `*` is rejected** (validator at `config.py`). |

Set via env:

```bash
DIGITORN_SERVER__HOST=0.0.0.0
DIGITORN_SERVER__PORT=9000
DIGITORN_SERVER__WORKERS=4
DIGITORN_SERVER__KV_BACKEND=redis://redis.internal:6379/0
DIGITORN_SERVER__CORS_ORIGINS='["https://my-frontend.com"]'
```

Or via YAML:

```yaml
server:
  host: 0.0.0.0
  port: 9000
  workers: 4
  kv_backend: redis://redis.internal:6379/0
  cors_origins:
    - https://my-frontend.com
```

## `auth:` - JWT and API keys

`config.py` `AuthConfig`. The full surface is documented in
[Auth](22-auth.md). Fields:

- `access_token_ttl`, `refresh_token_ttl` - informational; the
  central `digitorn-auth` service owns token TTLs.
- `max_login_failures` (default 5), `lockout_window` (default 900s).
- `approval_timeout` - time to wait for user approval before
  auto-deny (default 3600s).
- `mode` - must be `"remote"` when `auth_enabled: true`. The legacy
  `"embedded"` mode is rejected at startup (`server.py`).
- `service_url` - base URL of the central `digitorn-auth` service
  (required when `mode='remote'`).
- `accept_issuers` - extra `iss` values the daemon accepts.
- `enable_local_device` - opt-in offline pairing via
  `digitorn install-local`.

## `oauth:` - OAuth providers

`config.py` `OAuthConfig`. Each sub-section
(`OAuthProviderConfig`, `config.py`) is `{client_id,
client_secret}`. Set via env:

```bash
DIGITORN_OAUTH__GOOGLE__CLIENT_ID=...
DIGITORN_OAUTH__GOOGLE__CLIENT_SECRET=...
DIGITORN_OAUTH__MICROSOFT__CLIENT_ID=...
DIGITORN_OAUTH__MICROSOFT__CLIENT_SECRET=...
DIGITORN_OAUTH__GITHUB__CLIENT_ID=...
```

`public_base_url` (default `http://localhost:8000`) is used
to build the OAuth callback URL the daemon registers with
external providers. Set it to your daemon's externally-
reachable URL in prod.

## `database:` - Storage

`config.py` `DatabaseConfig`. SQLite by default; Postgres for
multi-host. Common fields:

- `url` - SQLAlchemy URL.
  `sqlite+aiosqlite:///path/to/db.sqlite` or
  `postgresql+asyncpg://user:pass@host/db`.
- `pool_size`, `max_overflow`, `pool_timeout`, `pool_recycle`
  - passed straight to SQLAlchemy.

```bash
DIGITORN_DATABASE__URL=postgresql+asyncpg://app:pwd@db.internal/digitorn
DIGITORN_DATABASE__POOL_SIZE=20
```

## `default_model:` - Default LLM for built-in apps

`config.py` `DefaultModelConfig`. Used by built-in apps
when no brain is configured.

- `provider`, `model`, `backend`, `api_key`, `base_url`,
  `temperature`, `max_tokens`, `context_window`

User-deployed apps **always** declare their own brain - this block
is purely the fallback for builtins.

```yaml
default_model:
  provider: deepseek
  model: deepseek-chat
  backend: openai_compat
  api_key: "{{env.DEEPSEEK_API_KEY}}"
  base_url: "https://api.deepseek.com/v1"
  temperature: 0.2
  context_window: 65536
```

## `session:` and `session_queue:` - Session lifecycle

`config.py` `SessionConfig` and `config.py`
`SessionQueueConfig`. Govern session timeouts, cleanup intervals,
and the background-mode activation queue (max in-flight, retry
policy, dead-letter handling).

## `images:` - Multimodal handling

`config.py` `ImageConfig`. Image-aging policy applied across
all apps with vision-capable agents:

- `max_per_message`, `max_size_bytes`
- `aging` - full resolution on the current turn, low-res (512 px)
  on turns 1-2, text description from turn 3
- `storage_dir` - where uploaded images are persisted

Documented in detail in `docs/spec-image-support.md`.

## `discovery:` - Semantic tool index

`config.py` `DiscoveryConfig`. Controls the FastEmbed +
Qdrant index used by `search_tools` in discovery mode.

- `skip_embeddings` - skip the semantic index (keyword-only fallback)
- `model_name` - sentence-transformer model (default
  `paraphrase-multilingual-MiniLM-L12-v2`, 384 dims, ~50 languages)
- `cache_dir` - where downloaded model weights live

## `mcp:` - MCP server pool

`config.py` `MCPConfig`. Connection-pool defaults shared
across all apps that use the MCP module.

- `pool_size`, `pool_max`
- `connect_timeout`, `request_timeout`
- `health_check_interval`

Per-server config lives in the **app** YAML
(`tools.modules.mcp.config.servers.*`); see
[MCP Servers](04d-mcp.md).

## `hub:` - Hub registry

`config.py` `HubConfig`. Connect the daemon to the public
Digitorn Hub for app catalog and credential templates.

- `base_url` - default `https://hub.digitorn.ai`
- `api_key` - optional, for publishing
- `auto_sync` - refresh the local catalog on a schedule

## `logging:` - Logs

`config.py` `LoggingConfig`.

- `level` - `DEBUG | INFO | WARNING | ERROR | CRITICAL`. Defaults
  to `INFO`. Override via `DIGITORN_LOG_LEVEL` env.
- `format` - log format string passed to `logging.Formatter`.
- `file` - optional log file path. When set, logs are tee'd to the
  file in addition to stdout.

## Layered loading - concrete example

Suppose three sources exist:

```yaml
# /etc/digitorn/config.yaml (system)
server:
  port: 8000
auth:
  service_url: "https://auth.shared.example.com"
```

```yaml
# ~/.digitorn/config.yaml (user)
server:
  host: 127.0.0.1
  port: 9000
```

```bash
# environment
export DIGITORN_SERVER__WORKERS=4
export DIGITORN_AUTH__SERVICE_URL=http://127.0.0.1:8001
```

The effective `Settings` after `Settings.load` are:

| Field | Effective value |
|-------|-----------------|
| `server.host` | `127.0.0.1` |
| `server.port` | `9000` |
| `server.workers` | `4` |
| `auth.service_url` | `https://auth.shared.example.com` |

If you don't want the system YAML to dictate, give the user YAML
the value to override (it sits at higher priority).

## Programmatic access

```python
from digitorn.core.config import get_settings, override_settings

# Read the effective settings
settings = get_settings()
print(settings.server.host, settings.server.port)
print(settings.auth.service_url)

# Override for tests
with override_settings(server={"port": 12345}):
    # use the overridden settings here
    ...
```

`get_settings` is the singleton accessor; `override_settings`
is a context manager used in the test suite.

## Cross-references

- Production hardening (TLS, CORS, rate limiting, sandbox):
  [Production Deployment](36-production.md)
- Auth deep dive (JWT, API keys, OAuth flow):
  [Auth](22-auth.md)
- Health and metrics endpoints:
  [Observability](24-observability.md)
- Per-app sandbox (different from the daemon-wide toggle):
  [Security → security.sandbox](11-security.md#securitysandbox---os-level-isolation)
