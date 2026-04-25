---

id: hub-isolation
title: Hub & Module Isolation
sidebar_position: 7
format: md
---



# Hub & Module Isolation

LLMOS Bridge supports community-authored modules through two complementary systems: the **Hub** for discovery, packaging, and installation, and the **Isolation** system for safely running untrusted code in subprocess sandboxes.

---


## Hub System

### Architecture

```
                    ┌──────────────────────────────────────────────┐
                    │                  Module Hub                   │
                    └──────────────────────────────────────────────┘
                                        |
              ┌─────────────────────────┼─────────────────────────┐
              |                         |                         |
    ┌─────────────────┐    ┌──────────────────────┐    ┌─────────────────┐
    │  ModulePackage   │    │    HubClient          │    │  ModuleValidator │
    │  (llmos-module   │    │    (HTTP, search,      │    │  (publish-ready  │
    │   .toml config)  │    │     download)          │    │   scoring)       │
    └─────────────────┘    └──────────────────────┘    └─────────────────┘
              |                         |                         |
              v                         v                         v
    ┌─────────────────┐    ┌──────────────────────┐    ┌─────────────────┐
    │  DependencyRes.  │    │    ModuleIndex         │    │  ModuleInstaller │
    │  (topological    │    │    (SQLite registry     │    │  (install,       │
    │   sort, PEP-440) │    │     of installed)       │    │   upgrade,       │
    └─────────────────┘    └──────────────────────┘    │   uninstall)      │
                                                        └─────────────────┘
                                                                  |
                                                                  v
                                                        ┌─────────────────┐
                                                        │  Signature       │
                                                        │  Verification    │
                                                        │  (Ed25519)       │
                                                        └─────────────────┘
```

---


### Module Package Format

Every community module is a directory containing `llmos-module.toml`:

#### ModulePackageConfig

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `module_id` | str | required | Unique module identifier |
| `version` | str | required | Semantic version |
| `description` | str | `""` | Module description |
| `author` | str | `""` | Author name |
| `license` | str | `""` | SPDX license identifier |
| `homepage` | str | `""` | Project URL |
| `module_class_path` | str | required | e.g. `my_module.module:MyModule` |
| `platforms` | list[str] | `["all"]` | Supported platforms |
| `requirements` | list[str] | `[]` | pip dependencies |
| `module_dependencies` | dict[str, str] | `{}` | Module version requirements (PEP-440) |
| `tags` | list[str] | `[]` | Discovery tags |
| `sandbox_level` | str | `"basic"` | Isolation level |
| `module_type` | str | `"user"` | Module type |
| `min_bridge_version` | str | `""` | Minimum daemon version |
| `icon` | str | `""` | Icon path |
| `actions` | list[ActionDeclaration] | `[]` | Declared actions |
| `capabilities` | CapabilityDeclaration | None | Declared capabilities |
| `docs` | DocsConfig | None | Documentation paths |

#### ActionDeclaration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | str | required | Action name |
| `description` | str | `""` | Description |
| `risk_level` | str | `"low"` | Risk classification |
| `permission` | str | `"local_worker"` | Minimum profile |
| `category` | str | `""` | Action category |

#### CapabilityDeclaration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `permissions` | list[str] | `[]` | OS permissions required |
| `side_effects` | list[str] | `[]` | Side effects (filesystem, network, etc.) |
| `events_emitted` | list[str] | `[]` | EventBus topics produced |
| `events_subscribed` | list[str] | `[]` | EventBus topics consumed |
| `services_provided` | list[str] | `[]` | ServiceBus services |
| `services_consumed` | list[str] | `[]` | ServiceBus dependencies |

---


### Hub Client

HTTP client for the LLMOS Module Hub registry.

| Method | Description |
|--------|-------------|
| `async search(query, limit)` | Search hub for modules |
| `async get_module_info(module_id)` | Get module details |
| `async download_package(module_id, version, dest)` | Download tarball |
| `async get_versions(module_id)` | List available versions |
| `async close()` | Close HTTP client |

Returns `HubModuleInfo` records:

| Field | Type | Description |
|-------|------|-------------|
| `module_id` | str | Module identifier |
| `version` | str | Latest version |
| `description` | str | Module description |
| `author` | str | Author name |
| `downloads` | int | Download count |
| `license` | str | SPDX license |
| `tags` | list[str] | Discovery tags |

---


### Module Index

SQLite-backed registry of locally installed community modules.

| Method | Description |
|--------|-------------|
| `async init()` | Create database and tables |
| `async add(module)` | Register installed module |
| `async remove(module_id)` | Unregister module |
| `async get(module_id)` | Get module by ID |
| `async list_all()` | List all installed modules |
| `async list_enabled()` | List only enabled modules |
| `async update_version(module_id, version, install_path)` | Update after upgrade |
| `async set_enabled(module_id, enabled)` | Enable/disable module |

#### InstalledModule

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `module_id` | str | required | Module identifier |
| `version` | str | required | Installed version |
| `install_path` | str | required | Filesystem path |
| `module_class_path` | str | required | Python class path |
| `requirements` | list[str] | `[]` | pip dependencies |
| `installed_at` | float | 0.0 | Install timestamp |
| `enabled` | bool | True | Whether active |
| `signature_fingerprint` | str | `""` | Ed25519 key fingerprint |
| `sandbox_level` | str | `"basic"` | Isolation level |

---


### Dependency Resolver

Topological sort of module-to-module dependencies with PEP-440 version checking.

| Method | Description |
|--------|-------------|
| `resolve(module_ids, package_configs)` | Resolve install order and dependencies |

#### ResolutionResult

| Field | Type | Description |
|-------|------|-------------|
| `install_order` | list[str] | Topologically sorted module IDs |
| `python_deps` | dict[str, list[str]] | Per-module pip requirements |
| `conflicts` | list[str] | Version conflicts found |
| `has_conflicts` | bool | True if any conflicts |

Uses Kahn's algorithm for topological sorting. Checks installed versions against PEP-440 specifiers.

---


### Module Installer

Orchestrates the full install/upgrade/uninstall lifecycle.

| Method | Description |
|--------|-------------|
| `async install_from_path(package_path)` | Install from local directory |
| `async install_from_hub(module_id, version, hub_client)` | Download and install from hub |
| `async uninstall(module_id)` | Uninstall module |
| `async upgrade(module_id, new_package_path)` | Upgrade to new version |
| `async verify_module(module_id)` | Verify integrity + signature |

#### InstallResult

| Field | Type | Description |
|-------|------|-------------|
| `success` | bool | Whether install succeeded |
| `module_id` | str | Module identifier |
| `version` | str | Installed version |
| `error` | str | Error message (if failed) |
| `installed_deps` | list[str] | Installed pip packages |

**Install pipeline**:
```
1. Parse llmos-module.toml → ModulePackageConfig
2. Verify signature (if require_signatures=true)
3. Resolve dependencies → install order
4. Create venv via VenvManager
5. Install pip requirements
6. Copy module to install_dir
7. Register in ModuleIndex (SQLite)
8. Register in ModuleRegistry (runtime)
```

---


### Module Validator

Validates module directory structure for hub publishing readiness. Scores 0-100.

| Check | Points | Description |
|-------|--------|-------------|
| llmos-module.toml exists and valid | 20 | Package config |
| module.py with BaseModule subclass | 15 | Implementation |
| params.py exists | 10 | Typed parameters |
| README.md with required sections | 20 | Documentation |
| CHANGELOG.md exists | 5 | Change history |
| docs/actions.md exists | 10 | Action reference |
| docs/integration.md exists | 5 | Integration guide |
| module_id consistency | 5 | ID matches config |
| version consistency | 5 | Version matches config |
| At least one action declared | 5 | Has functionality |

**Hub-ready**: score >= 70 and no blocking issues.

---


### Module Documentation

Parses module documentation from a directory.

| Method | Description |
|--------|-------------|
| `from_directory(module_dir)` | Load docs from directory |
| `sections()` | Parse README into named sections |
| `has_required_sections()` | Check for overview, actions, quick start, platform support |

---


## Module Isolation

### Architecture

```
LLMOS Bridge Daemon (Host Process)
    |
    +--→ IsolatedModuleProxy (extends BaseModule)
    |        |
    |        +--→ VenvManager.ensure_venv() → /path/to/python
    |        |
    |        +--→ subprocess.create_subprocess_exec(python, -m, worker)
    |        |        |
    |        |        +--→ Worker Process (isolated venv)
    |        |        |      |
    |        |        |      +--→ Load module class
    |        |        |      +--→ JSON-RPC 2.0 over stdin/stdout
    |        |        |      +--→ Execute actions in isolated environment
    |        |        |
    |        |        +--→ stdout: JSON-RPC responses
    |        |        +--→ stderr: structured logs
    |        |
    |        +--→ _reader_loop() reads responses
    |        +--→ _rpc(method, params) sends requests
    |
    +--→ HealthMonitor
             |
             +--→ Periodic health checks (configurable interval)
             +--→ Auto-restart on worker crash (max_restarts)
```

---


### JSON-RPC 2.0 Protocol

Communication between host and worker uses JSON-RPC 2.0 over stdin/stdout pipes.

#### Error Codes

| Code | Constant | Description |
|------|----------|-------------|
| -32700 | `PARSE_ERROR` | Invalid JSON |
| -32600 | `INVALID_REQUEST` | Invalid request structure |
| -32601 | `METHOD_NOT_FOUND` | Unknown RPC method |
| -32602 | `INVALID_PARAMS` | Invalid parameters |
| -32603 | `INTERNAL_ERROR` | Internal worker error |
| -32001 | `MODULE_LOAD_ERROR` | Module class failed to load |
| -32002 | `ACTION_NOT_FOUND_ERROR` | Action not found on module |
| -32003 | `ACTION_EXECUTION_ERROR` | Action raised exception |
| -32004 | `PERMISSION_ERROR` | Permission denied |
| -32005 | `TIMEOUT_ERROR` | Action timed out |

#### RPC Methods

| Method | Direction | Description |
|--------|-----------|-------------|
| `initialize` | Host → Worker | Load module class, return manifest |
| `execute` | Host → Worker | Execute action with params |
| `get_manifest` | Host → Worker | Return module manifest |
| `health_check` | Host → Worker | Return uptime and status |
| `shutdown` | Host → Worker | Graceful shutdown |

#### Notifications

| Method | Direction | Description |
|--------|-----------|-------------|
| `worker.ready` | Worker → Host | Worker initialized successfully |
| `worker.log` | Worker → Host | Structured log message |

#### Message Formats

```json
// Request (host → worker)
{"jsonrpc": "2.0", "method": "execute", "params": {"action": "read_file", "params": {"path": "/etc/hostname"}}, "id": "abc123"}

// Response (worker → host)
{"jsonrpc": "2.0", "result": {"content": "myhost\n"}, "id": "abc123"}

// Error response
{"jsonrpc": "2.0", "error": {"code": -32003, "message": "FileNotFoundError", "data": {"traceback": "..."}}, "id": "abc123"}

// Notification (worker → host, no id)
{"jsonrpc": "2.0", "method": "worker.log", "params": {"level": "info", "message": "Module loaded"}}
```

---


### Virtual Environment Manager

Manages per-module Python virtual environments for dependency isolation.

| Method | Description |
|--------|-------------|
| `has_uv()` | Check if `uv` CLI is available |
| `async ensure_venv(module_id, requirements)` | Return Python path (lazy-create venv) |
| `async remove_venv(module_id)` | Remove venv directory |
| `list_venvs()` | List module IDs with venvs |
| `venv_exists(module_id)` | Check if venv exists |
| `get_python(module_id)` | Return Python executable path |

**Venv creation**:
```
1. Compute SHA-256 of sorted requirements → hash
2. Check if .requirements.hash matches → skip creation
3. Create venv:
   a. uv available → `uv venv` + `uv pip install` (10x faster)
   b. Fallback → `python -m venv` + `pip install`
4. Write .requirements.hash for cache
```

**Directory structure**: `~/.llmos/venvs/{module_id}/` (configurable via `isolation.venv_base_dir`).

---


### Isolated Module Proxy

Host-side proxy that transparently forwards action calls to a subprocess worker.

Extends `BaseModule`, so the rest of the system treats it identically to an in-process module.

| Method | Description |
|--------|-------------|
| `async start()` | Create venv, spawn worker, handshake |
| `async stop()` | Graceful worker shutdown |
| `async restart()` | Stop then start (crash recovery) |
| `is_alive` (property) | Whether worker process is running |
| `async health_check()` | RPC health check to worker |
| `async execute(action, params, context)` | Forward via JSON-RPC |
| `get_manifest()` | Return cached manifest from worker |

**Crash recovery**: On worker crash, the proxy automatically restarts (up to `max_restarts` times with `restart_delay` backoff).

**Vision compatibility**: `parse_screen()` method forwards to worker for vision modules running in isolation.

---


### Health Monitor

Background task that periodically checks all isolated workers.

| Method | Description |
|--------|-------------|
| `register(proxy)` | Register proxy for monitoring |
| `async start()` | Start background health check loop |
| `async stop()` | Stop loop and all monitored workers |
| `async check_all()` | Run single health check round |

**Behavior on failure**:
```
health_check() fails for proxy X
    |
    +--→ proxy.restart_count < max_restarts
    |       → proxy.restart()
    |
    +--→ proxy.restart_count >= max_restarts
            → Log error, mark as dead
```

---


### Configuration

```yaml
isolation:
  enabled: false                    # Enable subprocess isolation
  default_isolation: subprocess     # in_process or subprocess
  venv_base_dir: ~/.llmos/venvs    # Venv storage
  prefer_uv: true                  # Use uv for faster venv creation
  health_check_interval: 10.0      # Seconds between checks
  modules:                          # Per-module isolation specs
    my_plugin:
      module_class_path: my_plugin.module:MyPlugin
      isolation: subprocess
      requirements:
        - numpy>=1.24
        - pandas>=2.0
      env_vars:
        MY_VAR: value
      timeout: 30.0
      max_restarts: 3
      restart_delay: 1.0
```
