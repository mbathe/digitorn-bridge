---

id: configuration
title: Configuration Reference
sidebar_position: 11
format: md
---



# Configuration Reference

LLMOS Bridge uses a layered configuration system based on Pydantic BaseSettings. All settings can be specified via YAML files, environment variables, or both.

---


## Loading Priority

```
Priority (highest to lowest):
1. Environment variables (LLMOS_SERVER__PORT=8080)
2. User config (~/.llmos/config.yaml)
3. System config (/etc/llmos-bridge/config.yaml)
4. Built-in defaults
```

**Environment variable format**: `LLMOS_` prefix with `__` (double underscore) as nested separator.

Examples:
- `LLMOS_SERVER__PORT=8080` → `server.port = 8080`
- `LLMOS_SECURITY__PERMISSION_PROFILE=power_user` → `security.permission_profile = "power_user"`
- `LLMOS_VISION__DEVICE=cuda` → `vision.device = "cuda"`

---


## Settings API

```python
from llmos_bridge.config import Settings, get_settings, override_settings

# Load settings (auto-discovers YAML files)
settings = Settings.load()

# Load from specific file
settings = Settings.load(config_file=Path("my-config.yaml"))

# Singleton access
settings = get_settings()  # Cached, auto-loads if None

# Override for testing
override_settings(custom_settings)

# Active modules (enabled minus disabled)
modules = settings.active_modules()  # ["filesystem", "os_exec", ...]
```

---


## Configuration Sections

### server

HTTP daemon settings.

| Field | Type | Default | Constraints | Description |
|-------|------|---------|-------------|-------------|
| `host` | str | `"127.0.0.1"` | — | Bind address |
| `port` | int | `40000` | 1024-65535 | Bind port |
| `workers` | int | `1` | 1-16 | Uvicorn worker count |
| `reload` | bool | `false` | — | Auto-reload on code change |
| `log_level` | str | `"info"` | debug/info/warning/error | Uvicorn log level |
| `sync_plan_timeout` | int | `300` | 10-3600 | Max wait for sync plan (seconds) |
| `rate_limit_per_minute` | int | `60` | 1-1000 | POST /plans rate limit |
| `max_result_size` | int | `524288` | 1024-10485760 | Max action result bytes |
| `plan_retention_hours` | int | `168` | 1-8760 | Auto-purge completed plans |

```yaml
server:
  host: 127.0.0.1
  port: 40000
  workers: 1
  log_level: info
  rate_limit_per_minute: 60
  max_result_size: 524288
```

---


### security

IML-level security policies.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `permission_profile` | str | `"local_worker"` | `readonly`, `local_worker`, `power_user`, `unrestricted` |
| `api_token` | str | None | Bearer token for all requests |
| `require_approval_for` | list[str] | 5 actions | Module.action patterns requiring approval |
| `max_plan_actions` | int | `50` | Max actions per plan (1-500) |
| `max_concurrent_plans` | int | `5` | Max simultaneous plans (1-20) |
| `sandbox_paths` | list[str] | `[]` | Restrict filesystem to these paths |
| `approval_timeout_seconds` | int | `300` | Approval wait time (10-3600) |
| `approval_timeout_behavior` | str | `"reject"` | `reject` or `skip` on timeout |

**Default approval-required actions**:
- `filesystem.delete_file`
- `filesystem.delete_directory`
- `os_exec.run_command`
- `os_exec.kill_process`
- `database.execute_query`

```yaml
security:
  permission_profile: local_worker
  api_token: null
  max_plan_actions: 50
  sandbox_paths:
    - /home/user/workspace
    - /tmp/llmos
  require_approval_for:
    - filesystem.delete_file
    - os_exec.run_command
```

---


### modules

Module loading configuration.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | list[str] | 13 modules | Modules to load |
| `disabled` | list[str] | `[]` | Override: disable these modules |
| `fallbacks` | dict[str, list[str]] | `{}` | Fallback chains |

**Default enabled modules** (13): filesystem, os_exec, api_http, excel, word, powerpoint, database, db_gateway, triggers, vision, gui, computer_control, window_tracker.

**Modules not enabled by default** (require opt-in):

| Module | Reason | How to Enable |
|--------|--------|---------------|
| `browser` | Requires `playwright` (heavy dependency + `npx playwright install`) | Add `browser` to `modules.enabled` |
| `iot` | Raspberry Pi / Linux only, requires GPIO hardware | Add `iot` to `modules.enabled` |
| `recording` | Opt-in subsystem, also requires `recording.enabled: true` | Add `recording` to `modules.enabled` + set `recording.enabled: true` |

Modules with missing dependencies will log a warning and be marked as `failed_load` without affecting the rest of the daemon.

```yaml
modules:
  enabled:
    - filesystem
    - os_exec
    - api_http
    - browser       # opt-in: requires playwright
    - iot           # opt-in: requires GPIO hardware
  disabled:
    - excel
  fallbacks:
    excel:
      - filesystem
```

---


### memory

State and vector storage.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `state_db_path` | Path | `~/.llmos/state.db` | SQLite KV store path |
| `vector_db_path` | Path | `~/.llmos/vector` | ChromaDB directory |
| `vector_enabled` | bool | `false` | Enable vector search |
| `max_history_entries` | int | `1000` | Max history records (100-100k) |

---


### perception

Basic perception settings (screenshot, OCR).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable perception pipeline |
| `ocr_enabled` | bool | `false` | Enable OCR extraction |
| `screenshot_format` | str | `"png"` | `png` or `jpeg` |
| `screenshot_quality` | int | `85` | JPEG quality (1-100) |

---


### vision

Visual perception configuration (OmniParser, Ultra backend).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `backend` | str | `"omniparser"` | `omniparser`, `ultra`, or class path |
| `model_dir` | str | `~/.llmos/models/omniparser` | OmniParser weights |
| `device` | str | `"auto"` | `auto`, `cpu`, `cuda`, `mps` |
| `box_threshold` | float | `0.05` | Detection confidence (0.0-1.0) |
| `iou_threshold` | float | `0.1` | NMS IOU threshold (0.0-1.0) |
| `caption_model_name` | str | `"florence2"` | `florence2` or `blip2` |
| `use_paddleocr` | bool | `true` | PaddleOCR vs EasyOCR |
| `auto_download_weights` | bool | `true` | Auto-download from HuggingFace |
| `cache_max_entries` | int | `5` | LRU cache size (0 = disabled) |
| `cache_ttl_seconds` | float | `2.0` | Cache TTL (0-60) |
| `speculative_prefetch` | bool | `true` | Background parse after actions |

**Ultra backend fields**:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `ultra_model_dir` | str | `~/.llmos/models/ultra_vision` | Ultra weights |
| `ultra_device` | str | `"auto"` | Compute device |
| `ultra_box_threshold` | float | `0.3` | Detection threshold |
| `ultra_ocr_engine` | str | `"paddleocr"` | OCR engine |
| `ultra_enable_grounding` | bool | `true` | UGround visual grounding |
| `ultra_grounding_idle_timeout` | float | `60` | Unload idle grounding model |
| `ultra_max_vram_mb` | int | `3000` | VRAM budget (500-24000) |
| `ultra_auto_download` | bool | `true` | Auto-download weights |

```yaml
vision:
  backend: omniparser
  device: cuda
  box_threshold: 0.05
  cache_max_entries: 5
  speculative_prefetch: true
```

---


### intent_verifier

LLM-based semantic security analysis.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Enable intent verification |
| `strict` | bool | `false` | Block on verification failure (vs log) |
| `provider` | str | `"null"` | `null`, `openai`, `anthropic`, `ollama`, `custom` |
| `model` | str | `""` | LLM model ID |
| `api_key` | str | None | Provider API key |
| `api_base_url` | str | None | Custom API base URL |
| `timeout_seconds` | float | `30` | Request timeout (5-120) |
| `cache_size` | int | `256` | LRU cache entries (0-10000) |
| `cache_ttl_seconds` | float | `300` | Cache TTL (0-3600) |
| `max_plan_actions_for_verification` | int | `50` | Skip large plans (1-500) |
| `skip_modules` | list[str] | `[]` | Module IDs to skip |
| `max_retries` | int | `2` | Retry on transient failure (0-5) |
| `custom_threat_categories` | list | `[]` | Additional threat categories |
| `disabled_threat_categories` | list[str] | `[]` | Disable specific categories |
| `custom_system_prompt_suffix` | str | `""` | Extra LLM instructions |
| `custom_provider_class` | str | None | Custom LLMClient class path |
| `custom_verifier_class` | str | None | Custom IntentVerifier class path |

```yaml
intent_verifier:
  enabled: true
  strict: false
  provider: anthropic
  model: claude-haiku-4-5-20251001
  api_key: sk-ant-...
  timeout_seconds: 30
  cache_size: 256
```

---


### scanner_pipeline

Pre-LLM heuristic and ML-based input screening.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Enable scanner pipeline |
| `fail_fast` | bool | `true` | Short-circuit on REJECT |
| `reject_threshold` | float | `0.7` | Risk score to reject (0.0-1.0) |
| `warn_threshold` | float | `0.3` | Risk score to warn (0.0-1.0) |
| `heuristic_enabled` | bool | `true` | Enable HeuristicScanner |
| `heuristic_disabled_patterns` | list[str] | `[]` | Pattern IDs to disable |
| `heuristic_extra_patterns` | list | `[]` | Custom regex patterns |
| `llm_guard_enabled` | bool | `false` | Enable LLMGuardScanner |
| `llm_guard_scanners` | list[str] | `["PromptInjection"]` | LLM Guard scanner names |
| `prompt_guard_enabled` | bool | `false` | Enable PromptGuardScanner |
| `prompt_guard_model` | str | `"meta-llama/Prompt-Guard-86M"` | Model ID |

**Custom pattern format**:
```yaml
scanner_pipeline:
  heuristic_extra_patterns:
    - id: custom_001
      category: custom
      pattern: "dangerous_keyword"
      severity: 0.8
      description: "Custom pattern"
```

---


### security_advanced

OS-level permission system.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `permissions_db_path` | Path | `~/.llmos/permissions.db` | SQLite permission store |
| `auto_grant_low_risk` | bool | `true` | Auto-grant LOW risk permissions |
| `enable_decorators` | bool | `true` | Runtime decorator enforcement |
| `enable_rate_limiting` | bool | `true` | Per-action rate limiting |

---


### triggers

Reactive automation system.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable trigger system |
| `db_path` | Path | `~/.llmos/triggers.db` | SQLite trigger store |
| `max_concurrent_plans` | int | `5` | Max triggered plans (1-50) |
| `max_chain_depth` | int | `5` | Max trigger chain depth (1-20) |
| `enabled_types` | list[str] | 5 types | Enabled trigger types |

---


### recording

Workflow recording and replay.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable recording |
| `db_path` | Path | `~/.llmos/recordings.db` | SQLite recording store |

---


### resources

Module concurrency control.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `default_concurrency` | int | `10` | Default per-module limit (1-100) |
| `module_limits` | dict[str, int] | `{}` | Per-module overrides |

```yaml
resources:
  default_concurrency: 10
  module_limits:
    excel: 3
    api_http: 20
    browser: 2
```

---


### db_gateway

Semantic database gateway.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_connections` | int | `10` | Connection pool size (1-50) |
| `default_row_limit` | int | `1000` | Default SELECT limit (1-100k) |
| `schema_cache_ttl` | int | `300` | Schema cache TTL (0-3600) |
| `auto_introspect` | bool | `true` | Auto-discover schema on connect |

---


### node

Distributed execution mode (Phase 4).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mode` | str | `"standalone"` | `standalone`, `node`, `orchestrator` |
| `node_id` | str | `"local"` | Node identifier |
| `location` | str | `""` | Human-readable location label |

---


### isolation

Module subprocess isolation.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable isolation system |
| `default_isolation` | str | `"subprocess"` | `in_process` or `subprocess` |
| `venv_base_dir` | str | `~/.llmos/venvs` | Venv storage directory |
| `prefer_uv` | bool | `true` | Use uv for 10x faster venv creation |
| `health_check_interval` | float | `10.0` | Health check interval (1-300) |
| `modules` | dict | `{}` | Per-module isolation specs |

**Per-module spec** (ModuleIsolationSpec):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `module_id` | str | required | Module identifier |
| `module_class_path` | str | required | Fully-qualified class path |
| `isolation` | str | `"subprocess"` | Isolation mode |
| `requirements` | list[str] | `[]` | pip requirements |
| `env_vars` | dict[str, str] | `{}` | Environment variables |
| `timeout` | float | `30` | Action timeout (5-300) |
| `max_restarts` | int | `3` | Max crash restarts (0-10) |
| `restart_delay` | float | `1.0` | Restart delay (0.1-30) |

---


### module_manager

Module management system.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Enable module manager |
| `allow_runtime_disable` | bool | `true` | Allow disabling modules at runtime |
| `allow_action_disable` | bool | `true` | Allow disabling individual actions |

---


### hub

Community module hub.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable hub integration |
| `registry_url` | str | `https://hub.llmos-bridge.io/api/v1` | Hub API URL |
| `trust_store_path` | Path | `~/.llmos/trust_store` | Ed25519 public keys |
| `require_signatures` | bool | `true` | Reject unsigned modules |
| `cache_dir` | Path | `~/.llmos/hub_cache` | Download cache |
| `auto_update` | bool | `false` | Auto-update modules |
| `install_dir` | Path | `~/.llmos/modules` | Module installation directory |

---


### logging

Logging configuration.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `level` | str | `"info"` | Log level |
| `format` | str | `"console"` | `console` or `json` |
| `file` | Path | None | Additional log file |
| `audit_file` | Path | `~/.llmos/audit.log` | Audit event log |

---


## Complete Example

```yaml
server:
  host: 127.0.0.1
  port: 40000
  log_level: info
  rate_limit_per_minute: 120

security:
  permission_profile: power_user
  sandbox_paths:
    - /home/user/workspace
  require_approval_for:
    - os_exec.run_command
    - os_exec.kill_process

modules:
  enabled:
    - filesystem
    - os_exec
    - api_http
    - excel
    - browser
    - gui
    - computer_control
    - window_tracker
    - vision
    - database

security_advanced:
  enable_decorators: true
  auto_grant_low_risk: true

intent_verifier:
  enabled: true
  provider: anthropic
  model: claude-haiku-4-5-20251001
  api_key: ${ANTHROPIC_API_KEY}

scanner_pipeline:
  enabled: true
  fail_fast: true

vision:
  backend: omniparser
  device: auto
  speculative_prefetch: true

triggers:
  enabled: true
  max_concurrent_plans: 5

recording:
  enabled: true

logging:
  level: info
  format: console
  audit_file: ~/.llmos/audit.log
```
