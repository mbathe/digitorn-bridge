---
id: configuration
title: Configuration Reference
sidebar_label: Configuration
sidebar_position: 5
description: All ~100 configuration parameters across 17 sections.
---

# Configuration Reference

Digitorn loads configuration from (in order of increasing priority):

1. **Built-in defaults** (hardcoded in `config.py`)
2. **System config**: `/etc/digitorn/config.yaml`
3. **User config**: `~/.digitorn/config.yaml`
4. **Environment variables**: prefixed with `DIGITORN_`

Nested env vars use double underscore as separator:
```bash
DIGITORN_SERVER__PORT=8000
DIGITORN_DATABASE__URL=postgresql+asyncpg://user:pass@localhost/digitorn
```

---

## server (13 params)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `host` | string | `"127.0.0.1"` | Server bind address |
| `port` | int | `8000` | Server port (1024-65535) |
| `workers` | int | `1` | Uvicorn worker count (1-16) |
| `reload` | bool | `false` | Auto-reload on code changes |
| `rate_limit_rpm` | int | `100000` | Default requests per minute per app (1-100000). Effectively disabled - set to the upper bound. Specific buckets (auth, admin, deploy) still have their own tighter caps in server.py. |
| `expose_docs` | bool | `false` | Expose Swagger UI (`/docs`), ReDoc (`/redoc`), and `/openapi.json`. Automatically `true` when `auth_enabled` is `false` (dev mode). Leave off in production. |
| `kv_backend` | string | `null` | KV backend URL. `redis://host:6379/0` for production. Default (null) uses DiskCache (SQLite-backed, single-host). |
| `auth_enabled` | bool | `true` | Enable JWT/API-key authentication on all API endpoints |
| `turn_workers` | int | `32` | Thread pool size for agent turns (1-512) |
| `io_workers` | int | `64` | Thread pool size for blocking I/O (1-1024) |
| `sandbox` | bool | `true` | Enable OS-level sandbox for deployed apps (Landlock/seccomp on Linux, Seatbelt on macOS, Job Objects on Windows) |
| `node_auto_install` | bool | `true` | Auto-download Node.js LTS runtime if missing (PATH / nvm / fnm / volta are checked first). Disable in air-gapped or CI environments. |
| `cors_origins` | list[str] | localhost variants | Allowed CORS origins. Wildcard `*` is rejected. |

---

## database (3 params)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | string | `"sqlite+aiosqlite:///digitorn.db"` | SQLAlchemy async database URL (SQLite, PostgreSQL, MySQL) |
| `echo` | bool | `false` | Echo SQL statements to stdout (debug only) |
| `pool_size` | int | `5` | Connection pool size (1-50, ignored for SQLite) |

---

## auth (9 params)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `access_token_ttl` | int | `0` | Access token lifetime in seconds. `0` = never expires (no `exp` claim). Default is `0` for local-dev ergonomics - set to a positive value (e.g. `900` for 15 min) in production. |
| `refresh_token_ttl` | int | `0` | Refresh token lifetime in seconds. `0` = never expires. See `access_token_ttl` for rationale. |
| `max_login_failures` | int | `5` | Lock account after N failed login attempts (1-100) |
| `lockout_window` | int | `900` | Lockout window in seconds (60-86400) |
| `approval_timeout` | float | `3600.0` | Time to wait for user approval before auto-deny (10-7200s). Override per-app via the compiled `security_profile.approval_timeout`. |
| `mode` | string | `"embedded"` | **Must be set to `"remote"` when `auth_enabled=true`.** The schema default `"embedded"` is rejected at startup (`server.py`) - the daemon never signs tokens, only verifies. |
| `service_url` | string | `""` | Base URL of the central `digitorn-auth` service (e.g. `https://auth.digitorn.ai`). Required when `mode='remote'`. |
| `accept_issuers` | list[str] | `[]` | Extra `iss` claim values the daemon accepts (cluster + edge proxy + dev loopback). |
| `enable_local_device` | bool | `false` | Load `LocalDeviceAuth` secrets at daemon start and run the device revalidator background task. Requires the daemon to have been paired via `digitorn install-local`. Only meaningful in `mode='remote'`. |

---

## session (6 params + queue sub-section)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `idle_ttl` | int | `1800` | Session expires after N seconds of inactivity (default: 30 min). `0` = never expire. |
| `absolute_ttl` | int | `86400` | Session expires after N seconds regardless of activity (default: 24h). `0` = never expire. |
| `max_events_per_turn` | int | `500` | Safety cap on events emitted per turn (50-5000) |
| `max_sessions_per_app` | int | `100` | Max active sessions per deployed app (1-10000) |
| `lock_timeout` | float | `600.0` | Seconds a new turn waits for the session lock before returning `session_busy` (5-3600). When the queue is enabled, incoming messages queue instead of erroring out. |
| `approval_timeout_s` | float | `300.0` | How long an agent waits for a user to resolve an approval request before auto-denying (5-86400s). Apps with a compiled `security_profile.approval_timeout` override this default. |

> **Disabling session expiry**: Setting `idle_ttl: 0` or `absolute_ttl: 0` disables the
> respective timeout - sessions become permanent until explicitly deleted.
> The built-in defaults are `idle_ttl: 1800` (30 min) and `absolute_ttl: 86400` (24h).
> Set both to `0` in your `~/.digitorn/config.yaml` if you want permanent sessions.

### session.queue (6 params)

Controls the per-session message queue. When enabled, messages sent while a turn is running are enqueued in a persistent FIFO instead of returning `session_busy`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `enabled` | bool | `true` | Feature flag. When `false`, falls back to the legacy lock-based flow. |
| `max_depth` | int | `20` | Max queued messages per session. Over the cap, POST /messages returns 429. |
| `ttl_seconds` | int | `3600` | Queued messages auto-expire after N seconds (default: 1h). |
| `auto_merge` | bool | `false` | Concatenate consecutive user messages sent within `auto_merge_window_s` into a single turn (saves LLM calls on rapid follow-ups). |
| `auto_merge_window_s` | float | `2.0` | Window in seconds for `auto_merge` (0.5-30). |
| `default_mode` | string | `"async"` | How POST /messages behaves: `"async"` = enqueue + return 202 (recommended); `"wait"` = block until turn finishes (legacy compat). |

---

## runtime (8 params)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_consecutive_failures` | int | `8` | Consecutive tool failures before warning (1-30) |
| `max_repeat_window` | int | `20` | Sliding window size for duplicate tool-call detection (2-100) |
| `max_repeats` | int | `8` | Max identical calls within window before warning (1-30) |
| `max_consecutive_same_tool` | int | `30` | Max consecutive calls to the same tool before warning (1-100) |
| `tool_timeout` | float | `3600.0` | Default per-tool execution timeout in seconds (1-7200) |
| `context_pressure_threshold` | float | `0.75` | Token pressure ratio that triggers compaction (0.1-0.99) |
| `specialist_context_window` | int | `50000` | Default context window size for specialist agents (4000-2000000) |
| `watch_poll_interval` | int | `5` | File watch trigger polling interval in seconds (1-300) |

---

## agent_spawn (4 params)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_workers` | int | `3` | Max parallel sub-agents per session (1-50) |
| `max_turns` | int | `100` | Default max turns per sub-agent (10-10000) |
| `timeout` | float | `3600.0` | Default sub-agent timeout in seconds (30-7200) |
| `cleanup_age` | float | `300.0` | Remove completed sub-agents after N seconds (30-86400) |

---

## mcp (4 params)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `health_interval` | int | `60` | Health check interval for MCP servers in seconds (10-600) |
| `process_timeout` | float | `60.0` | MCP subprocess execution timeout in seconds (5-600) |
| `max_reconnect_attempts` | int | `4` | Max reconnect attempts for failed MCP servers (0-20) |
| `tool_call_timeout` | float | `3600.0` | Timeout for individual MCP tool calls in seconds (5-7200) |

---

## sandbox (4 params)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `pool_size` | int | `2` | Number of warm sandbox workers (1-20) |
| `idle_timeout` | float | `60.0` | Idle sandbox worker cleanup timeout in seconds (10-600) |
| `max_processes` | int | `10` | Max processes per sandbox (1-100) |
| `drain_timeout` | float | `30.0` | Shutdown drain timeout in seconds (5-120) |

---

## websocket (2 params)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `rate_limit_window` | float | `10.0` | Rate limit window in seconds (1-60) |
| `rate_limit_max_connections` | int | `5` | Max WebSocket connections per IP within the window (1-100) |

---

## default_model (8 params)

Default LLM model for built-in apps. These values are
injected into built-in app YAMLs at deploy time.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `provider` | string | `"anthropic"` | LLM provider: `anthropic`, `deepseek`, `openai`, `ollama`, etc. |
| `model` | string | `"claude-sonnet-4-20250514"` | Model name/ID |
| `backend` | string | `""` | Backend override: `openai_compat`, etc. Empty = auto-detect. |
| `api_key` | string | `"claude-code"` | API key. Special: `claude-code` (OAuth token), `env:VAR_NAME` (env var), or literal key. |
| `base_url` | string | `""` | Base URL override for the API |
| `temperature` | float | `0.7` | Sampling temperature (0.0-2.0) |
| `max_tokens` | int | `4096` | Max output tokens (256-65536) |
| `context_window` | int | `200000` | Context window size (4000-2000000) |

---

## discovery (6 params)

Tool discovery and semantic search settings (for discovery mode).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `embedding_model` | string | `"sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"` | FastEmbed model ID |
| `embedding_dim` | int | `384` | Embedding vector dimension (64-4096) |
| `skip_embeddings` | bool | `false` | Skip semantic index (saves ~900MB RAM, keyword search only) |
| `search_top_k` | int | `5` | Default number of results for tool search (1-50) |
| `search_min_score` | float | `0.2` | Minimum similarity score for search results (0.0-1.0) |
| `models_cache_dir` | string | `""` | Cache directory for embedding models. Empty = `~/.local/share/digitorn/models/` |

---

## modules (4 params)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `paths` | list[str] | `["modules"]` | Directories to scan for modules |
| `enabled` | list[str] | `[]` | Module IDs to load. Empty = load all discovered. |
| `disabled` | list[str] | `[]` | Module IDs to exclude (overrides `enabled`) |
| `load_all` | bool | `true` | Load all discovered modules with `enabled=true` in their TOML |

---

## app (3 params)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `yaml_path` | string | `null` | App YAML to compile and bootstrap at startup |
| `stop_on_error` | bool | `false` | Stop bootstrap if any setup step fails |
| `hot_reload` | bool | `false` | Dev mode - watch each deployed app's `prompts/`, `skills/`, and `assets/` directories and auto-redeploy on change. Keep `false` in production. |

---

## images (8 params)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_per_message` | int | `10` | Max images per message (0 = unlimited) |
| `max_size_bytes` | int | `10485760` | Max size per image (10MB) |
| `max_per_session` | int | `100` | Max images per session (0 = unlimited) |
| `storage_dir` | string | `""` | Image storage directory. Empty = `~/.digitorn/images/` |
| `low_res_size` | int | `512` | Resize dimension for aged images (px) |
| `aging_full_turns` | int | `1` | Turns an image stays at full resolution |
| `aging_low_turns` | int | `2` | Turns at low resolution before text-only |
| `cleanup_after_days` | int | `7` | Auto-delete images after N days |


## logging (2 params)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `level` | string | `"info"` | Log level: `debug`, `info`, `warning`, `error`, `critical` |
| `format` | string | `"console"` | Output format: `json` or `console` |


## transcribe (11 params)

Voice-to-text pipeline. Two providers:

- `local` - `faster-whisper` in-process. Install with `pip install digitorn[transcribe]`. Model weights downloaded + cached on first use (~150 MB for `base`, ~500 MB for `small`).
- `openai` - OpenAI Whisper API. **API key is read from the credentials system** (provider=`openai`, field=`api_key`), NOT from `config.yaml`. See [Credentials](#openai-api-key-for-transcribe).

The local model is **a singleton shared across all apps and sessions** - one copy in RAM/VRAM, not `N × apps`. Two loading strategies are selectable:

- `preload: true` (default) - model loaded eagerly during daemon startup in a background task. The daemon still starts serving HTTP immediately; the first transcribe request is instant once preload completes. Recommended for production.
- `preload: false` - lazy-load on the first transcribe request. Startup memory is lower but the first call waits 2–10 s (model download/load).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `enabled` | bool | `true` | If `false`, endpoint returns 404 (client falls back to attaching audio to the next message). |
| `provider` | string | `"local"` | `"local"` (faster-whisper) or `"openai"` (Whisper-1 API). |
| `model` | string | `"base"` | faster-whisper size: `tiny`, `base`, `small`, `medium`, `large-v3`. Ignored when `provider=openai`. |
| `device` | string | `"auto"` | `"cpu"`, `"cuda"`, or `"auto"`. Local provider only. |
| `compute_type` | string | `"int8"` | faster-whisper compute: `int8` (CPU), `int8_float16`/`float16` (CUDA), `float32`. |
| `max_audio_bytes` | int | `26214400` | Hard upload cap (25 MB). Requests above this return 413. |
| `min_audio_bytes` | int | `500` | Below this, the audio is rejected as empty/truncated (422). |
| `timeout_seconds` | float | `120.0` | Max inference time for a single upload. |
| `preload` | bool | `true` | Load the model eagerly at daemon startup (instant first request). Set `false` for lazy-load. Ignored when `provider=openai`. |
| `shared_instance` | bool | `true` | One model instance for all apps/sessions. Leave `true` unless you need per-session isolation (memory cost scales linearly if `false`). |
| `max_concurrency` | int | `1` | Max simultaneous transcriptions per model instance. Extra requests queue. Bump on a GPU with multi-stream inference. |

---

## hub (3 params)

Remote Digitorn Hub integration. When `url` is empty (the default), hub integration is disabled and `source_type: hub` package installs return `501`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | string | `""` | Base URL of the remote hub (e.g. `https://hub.digitorn.ai`). Empty = disabled. |
| `verify_ssl` | bool | `true` | Verify TLS certificates when connecting to the hub. |
| `timeout_seconds` | float | `60.0` | HTTP timeout for hub requests in seconds (1-600). |

---

## Example config.yaml

```yaml
server:
  host: "0.0.0.0"
  port: 8000
  workers: 4
  auth_enabled: true
  sandbox: true
  kv_backend: "redis://localhost:6379/0"
  cors_origins:
    - "https://myapp.example.com"

database:
  url: "postgresql+asyncpg://digitorn:secret@localhost/digitorn"
  pool_size: 10

auth:
  access_token_ttl: 3600
  approval_timeout: 600.0

session:
  idle_ttl: 3600
  max_sessions_per_app: 200

runtime:
  context_pressure_threshold: 0.80
  tool_timeout: 180.0

agent_spawn:
  max_workers: 5
  timeout: 1800.0

default_model:
  provider: anthropic
  model: claude-sonnet-4-20250514
  api_key: "claude-code"
  context_window: 200000

logging:
  level: info
  format: json

transcribe:
  enabled: true
  provider: local     # or "openai" + export OPENAI_API_KEY=...
  model: base         # tiny | base | small | medium | large-v3
  device: auto        # cpu | cuda | auto
  compute_type: int8  # int8 (CPU) | int8_float16 (CUDA) | float16 | float32
  max_audio_bytes: 26214400
  timeout_seconds: 120.0
```

### Environment variable overrides

All **non-secret** fields honour the `DIGITORN_<SECTION>__<FIELD>`
convention (double underscore between section and field):

```bash
export DIGITORN_TRANSCRIBE__PROVIDER=openai
export DIGITORN_TRANSCRIBE__MODEL=small
export DIGITORN_TRANSCRIBE__ENABLED=false
```

Restart the daemon after changes: `digitorn service restart`.
Verify with the transcribe health-check (admin reference) -
`ready: true` means the provider is loaded and the endpoint
is serving.

### <a id="openai-api-key-for-transcribe"></a>OpenAI API key for `transcribe`

Secrets **never go in `config.yaml` or env vars**. Digitorn has a
dedicated encrypted credentials store (`credentials` table, AES at
rest, 4-scope resolution). Use it:

```bash
# Via CLI - stores at system scope (available to all users)
digitorn credentials set openai api_key sk-... --scope system

# Or per-user (only this user's sessions can transcribe)
digitorn credentials set openai api_key sk-... --scope user

# Programmatically: use the Python testing SDK or the
# CLI; the REST endpoints are not documented publicly.
```

Resolution order at request time (first hit wins):
1. per-user + per-app
2. per-user
3. per-app (shared)
4. system-wide
5. `OPENAI_API_KEY` env var - **dev/CI fallback only**

Verify by querying the transcribe health surface (admin
reference). It returns `{enabled, provider, ready, error}`.
`ready:false` with `"OpenAI API key not configured..."`
means no credential in any scope - add one and restart.