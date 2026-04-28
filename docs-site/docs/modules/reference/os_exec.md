---

id: os_exec
title: OS Exec Module
sidebar_position: 2
format: md
---




# os_exec

Process execution, system information retrieval, and application lifecycle management. All commands are executed as lists - never as shell strings.

| Property | Value |
|----------|-------|
| **Module ID** | `os_exec` |
| **Version** | `1.0.0` |
| **Type** | system |
| **Platforms** | All |
| **Dependencies** | `psutil` |
| **Declared Permissions** | `process.execute`, `process.kill` |

---


## Actions

### run_command

Execute a system command. Commands must be provided as a list of arguments - shell strings are never accepted.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `command` | array | Yes | - | Command as list: `["ls", "-la", "/tmp"]` |
| `timeout` | integer | No | `60` | Execution timeout in seconds |
| `cwd` | string | No | `null` | Working directory |
| `env` | object | No | `null` | Additional environment variables |

**Returns**: `{"stdout": "...", "stderr": "...", "return_code": 0, "duration_seconds": 1.2}`

**Security**:
- `@requires_permission(Permission.PROCESS_EXECUTE)`
- `@sensitive_action(RiskLevel.MEDIUM)`
- `@rate_limited(calls_per_minute=30)`
- `@audit_trail("detailed")`

**Critical security constraint**: The command is always `subprocess.run(command_list, shell=False)`. Shell injection is impossible by design.

---


### list_processes

List running processes with optional name filter.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `name_filter` | string | No | `null` | Filter by process name (substring match) |

**Returns**: `{"processes": [{"pid": 1234, "name": "python", "cpu_percent": 2.5, "memory_mb": 50.0, ...}]}`

**Security**: Read-only, no special permissions in default profile.

---


### kill_process

Terminate a process by PID.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pid` | integer | Yes | - | Process ID |
| `signal` | string | No | `"SIGTERM"` | `SIGTERM` (graceful) or `SIGKILL` (force) |

**Security**:
- `@requires_permission(Permission.PROCESS_KILL)`
- `@sensitive_action(RiskLevel.HIGH, irreversible=True)`
- `@audit_trail("detailed")`

---


### get_process_info

Get detailed information about a specific process.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pid` | integer | Yes | - | Process ID |

**Returns**: `{"pid": 1234, "name": "python", "status": "running", "cpu_percent": 2.5, "memory_mb": 50.0, "cmdline": [...], "cwd": "...", "create_time": "..."}`

---


### open_application

Launch an application with arguments.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `application` | string | Yes | - | Application name or path |
| `args` | array | No | `[]` | Arguments |

**Returns**: `{"pid": 5678, "application": "firefox"}`

**Security**:
- `@requires_permission(Permission.PROCESS_EXECUTE)`

---


### close_application

Close an application by name.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `name` | string | Yes | - | Application name |
| `force` | boolean | No | `false` | Force kill if graceful close fails |

**Security**:
- `@requires_permission(Permission.PROCESS_KILL)`
- `@sensitive_action(RiskLevel.MEDIUM)`

---


### set_env_var

Set an environment variable in the daemon process.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `name` | string | Yes | - | Variable name |
| `value` | string | Yes | - | Variable value |

---


### get_env_var

Get the value of an environment variable.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `name` | string | Yes | - | Variable name |

**Returns**: `{"name": "HOME", "value": "/home/user"}`

---


### get_system_info

Get comprehensive system information.

**Returns**:
```json
{
  "os": "Linux",
  "release": "6.1.0-generic",
  "machine": "x86_64",
  "python_version": "3.11.5",
  "cpu_count": 8,
  "memory_total_mb": 16384,
  "memory_available_mb": 8192,
  "disk_total_gb": 500,
  "disk_free_gb": 250
}
```

---


## Streaming Support

The `run_command` action is decorated with `@streams_progress` and emits real-time events via SSE (`GET /plans/{plan_id}/stream`):

| Action | Status Phases | Progress Pattern |
|--------|---------------|------------------|
| `run_command` | `starting_process` → `running` | 100% on exit with exit code |

Other actions (`list_processes`, `get_system_info`, etc.) complete near-instantly and are not streaming-enabled.

See [Decorators Reference - @streams_progress](../../annotators/decorators.md) for SDK consumption details.

---


## Implementation Notes

- Uses `psutil` for process management and system information
- `subprocess.run()` with `shell=False` exclusively
- New sessions via `start_new_session=True` for `open_application`
- Timeout enforcement via `subprocess.TimeoutExpired`
