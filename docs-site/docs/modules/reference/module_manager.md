---

id: module_manager
title: Module Manager Module
sidebar_position: 3
format: md
---




# module_manager

Runtime module governance. Manage the lifecycle of all modules: enable, disable, pause, resume, restart. Monitor health and metrics. Search and install modules from the LLMOS Hub.

| Property | Value |
|----------|-------|
| **Module ID** | `module_manager` |
| **Version** | `2.0.0` |
| **Type** | system |
| **Platforms** | All |
| **Dependencies** | None (hub operations require `hub.enabled = true`) |
| **Declared Permissions** | `module.read`, `module.manage`, `module.install` |

---


## Actions (22)

### Module Discovery

| Action | Description | Key Parameters | Security |
|--------|-------------|----------------|----------|
| `list_modules` | List modules with filters | `state`, `type`, `include_health` | `@requires_permission(Permission.MODULE_READ)` |
| `get_module_info` | Module details, actions, health, metrics | `module_id` | `@requires_permission(Permission.MODULE_READ)` |
| `describe_module` | Rich description (README, examples) | `module_id` | |
| `get_system_status` | Overall system health | | |

### Lifecycle Management

| Action | Description | Key Parameters | Security |
|--------|-------------|----------------|----------|
| `enable_module` | Enable module | `module_id` | `@requires_permission(Permission.MODULE_MANAGE)` |
| `disable_module` | Disable module | `module_id` | `@requires_permission(Permission.MODULE_MANAGE)` |
| `pause_module` | Temporarily pause | `module_id` | `@requires_permission(Permission.MODULE_MANAGE)` |
| `resume_module` | Resume paused module | `module_id` | `@requires_permission(Permission.MODULE_MANAGE)` |
| `restart_module` | Restart module | `module_id` | `@requires_permission(Permission.MODULE_MANAGE)` |

**Protected modules**: System modules (`filesystem`, `os_exec`, `security`, `module_manager`) cannot be disabled or uninstalled.

### Action Management

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `enable_action` | Enable specific action | `module_id`, `action_name` |
| `disable_action` | Disable specific action | `module_id`, `action_name` |

### Monitoring

| Action | Description | Returns |
|--------|-------------|---------|
| `get_module_health` | Health check | `{status, latency_ms, error_rate, last_error}` |
| `get_module_metrics` | Operational metrics | `{call_count, avg_latency_ms, error_count, uptime_seconds}` |
| `get_module_state` | Current state | `active`, `disabled`, `paused`, `error`, `failed_load`, `deprecated` |

### Configuration

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `update_module_config` | Update module configuration | `module_id`, `config` (dict) |

### Inter-Module Services

| Action | Description | Returns |
|--------|-------------|---------|
| `list_services` | List all registered services | `{services: [{name, methods, provider_module}]}` |

### Hub Integration

| Action | Description | Key Parameters | Security |
|--------|-------------|----------------|----------|
| `search_hub` | Search LLMOS Module Hub | `query`, `tags`, `limit` | |
| `install_module` | Install from pip or hub | `package`, `source` (`pip`/`hub`) | `@requires_permission(Permission.MODULE_INSTALL)` |
| `uninstall_module` | Uninstall module | `module_id` | `@requires_permission(Permission.MODULE_INSTALL)` |
| `upgrade_module` | Upgrade to newer version | `module_id`, `version` | `@requires_permission(Permission.MODULE_INSTALL)` |
| `list_installed` | List installed with versions | | |
| `verify_module` | Verify integrity (checksum, Ed25519 signature) | `module_id` | |

---


## Module States

```
                    ┌── enable_module ──→ ACTIVE
                    |                      |
REGISTERED ─────────┤                   pause_module
                    |                      |
                    └── (load error) ──→ FAILED_LOAD
                                          v
                                       PAUSED
                                          |
                                       resume_module
                                          |
                                          v
                                       ACTIVE
                                          |
                                   ┌── disable_module ──→ DISABLED
                                   |
                                   └── error ──→ ERROR
                                                   |
                                                restart_module
                                                   |
                                                   v
                                                ACTIVE
```

| State | Description |
|-------|-------------|
| `active` | Running, accepting actions |
| `disabled` | Stopped by administrator |
| `paused` | Temporarily suspended |
| `error` | Runtime error occurred |
| `failed_load` | Could not initialize (missing dependencies, etc.) |
| `deprecated` | Marked for removal |

---


## Module Types

| Type | Description | Examples |
|------|-------------|---------|
| `system` | Core built-in modules | filesystem, os_exec, security |
| `plugin` | Official extensions | database_gateway |
| `community` | Third-party modules | Custom modules from hub |
| `premium` | Commercial modules | Enterprise-specific |

---


## Implementation Notes

- Uses ModuleLifecycleManager for state transitions
- Uses ServiceBus for inter-module service discovery
- Hub integration requires `hub.enabled = true` and HubClient, ModuleIndex, SignatureVerifier, VenvManager components
- Module installation runs in isolated virtual environments (via IsolationManager)
- Ed25519 signature verification for hub-installed modules
