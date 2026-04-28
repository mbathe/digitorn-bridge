# Shell Module - Action Reference

1 ultra-powerful bash action with 4 execution modes: sync, async, status, and kill.

Every action is designed for AI agents: commands are confined to the workspace,
outputs include exit codes, and sensitive values are always masked.

---

## bash

Execute a shell command in 1 of 4 modes:

| Mode | Parameters | Returns | Use case |
|------|-----------|---------|----------|
| **Sync** | command | stdout, stderr, exit_code | Normal commands (default) |
| **Async** | command, run_in_background=true | task_id, status | Long-running processes |
| **Status** | task_id | status, uptime, output tail | Check background task |
| **Kill** | task_id, kill=true | status, exit_code | Terminate background task |

**Permissions:** `process.exec`
**Risk level:** High
**Platform:** All (bash on Linux/macOS, Git Bash/PowerShell/cmd.exe on Windows)

### Parameters

| Name | Type | Required | Default | Visible | Description |
|------|------|----------|---------|---------|-------------|
| `command` | string | No | - | Yes | Shell command to execute (for sync/async modes) |
| `description` | string | No | "" | Yes | Label shown in UI |
| `run_in_background` | bool | No | False | Yes | Launch async (Async mode) |
| `task_id` | string | No | - | **Hidden** | Task ID for status/kill (Status/Kill modes) |
| `kill` | bool | No | False | **Hidden** | Kill the task (Kill mode) |
| `timeout` | float | No | 30.0 | **Hidden** | Max time to wait in seconds (1–300) |
| `cwd` | string | No | "." | **Hidden** | Working directory (must be inside workspace) |

---

## Mode 1: Sync Execution

Execute a command and wait for the result.

### Usage

```
Bash(command='ls -la')
Bash(command='pytest tests/', timeout=60.0)
```

### Returns

```json
{
  "stdout": "total 42\ndrwxr-xr-x...\n",
  "stderr": "",
  "exit_code": 0,
  "command": "ls -la",
  "cwd": "/workspace"
}
```

### Blocking patterns

- `sleep` > 2 seconds → rejected (use run_in_background=true instead)
- `sed -i` → rejected (use filesystem.edit() instead)

### Errors

| Error | Cause |
|-------|-------|
| `Command rejected: matches forbidden pattern '...'` | Command matches the platform blacklist |
| `cwd '...' is outside the allowed workspace` | cwd escapes the workspace root |
| `Timed out after Ns` | Execution exceeded timeout |

---

## Mode 2: Async Execution

Launch a long-running command in the background and return immediately.

### Usage

```
Bash(command='npm start', run_in_background=true)
Bash(command='python3 train.py', run_in_background=true)
```

### Returns (success)

```json
{
  "task_id": "a1b2c3d4e5f6",
  "pid": 12345,
  "command": "npm start",
  "status": "running",
  "message": "Task 'a1b2c3d4e5f6' started (PID 12345). Use Bash(task_id='a1b2c3d4e5f6') to check."
}
```

### Returns (immediate failure)

```json
{
  "task_id": "a1b2c3d4e5f6",
  "status": "failed",
  "exit_code": 1,
  "stderr": "Address already in use"
}
```

### How it works

1. Subprocess spawned via `asyncio.create_subprocess_*` (truly non-blocking)
2. 300ms stabilization check detects immediate failures (port in use, command not found, permission denied)
3. If process crashes within 300ms: error returned immediately instead of false "running" status
4. Otherwise: task_id returned, process continues in background
5. Output streamed into deque (max 10,000 lines per stream)

---

## Mode 3: Status Checking

Check the status and output of a background task.

### Usage

```
Bash(task_id='a1b2c3d4e5f6')
```

### Returns

```json
{
  "task_id": "a1b2c3d4e5f6",
  "command": "npm start",
  "status": "running",
  "exit_code": null,
  "uptime_seconds": 45.2,
  "stdout": "Server listening on port 3000\nRequest from 127.0.0.1\n...",
  "stderr": "",
  "stdout_total_lines": 42,
  "stderr_total_lines": 0
}
```

Returns **last 50 lines** of output (tail). For full output, use larger tail parameter.

### Returns (finished task)

```json
{
  "task_id": "a1b2c3d4e5f6",
  "command": "pytest tests/",
  "status": "finished",
  "exit_code": 0,
  "uptime_seconds": 12.3,
  "stdout": "...",
  "stderr": "",
  "stdout_total_lines": 120,
  "stderr_total_lines": 0
}
```

### Periodic cleanup

Tasks finished more than 1 hour ago are automatically cleaned up to prevent unbounded dict growth.

---

## Mode 4: Kill

Terminate a background task (graceful SIGTERM → forceful SIGKILL).

### Usage

```
Bash(task_id='a1b2c3d4e5f6', kill=true)
```

### Returns

```json
{
  "task_id": "a1b2c3d4e5f6",
  "status": "killed",
  "exit_code": -15
}
```

### How it works

1. Send SIGTERM (graceful shutdown)
2. Wait 5 seconds for process to exit
3. If still running: send SIGKILL (forceful termination)
4. Return final exit code

---

## Examples

### Example 1: Run tests with timeout

```yaml
Bash(command='pytest tests/ -v', timeout=120.0)
```

### Example 2: Long-running build in background

```yaml
# Start build
Bash(command='npm run build', run_in_background=true)
# Returns: {task_id: 'a1b2c3d4e5f6', ...}

# Check progress (multiple times)
Bash(task_id='a1b2c3d4e5f6')

# When done, check exit code
# If exit_code=0: success
# If exit_code≠0: build failed
```

### Example 3: Web server in background

```yaml
# Start server
Bash(command='python3 -m http.server 8000', run_in_background=true)

# Wait and check it started
Bash(task_id='<task_id>')

# If status='running': server is ready
# If status='failed': port in use or other error
```

### Example 4: Interactive input (not supported)

The consolidated bash action does NOT support stdin input. For interactive commands,
use a subprocess library directly or write a script file.

---

## Security

- **Command blacklist** - platform-specific patterns (fork bombs, disk wipes, etc.)
- **Workspace confinement** - cwd always resolved inside allowed root
- **Output size cap** - stdout/stderr truncated at configurable max_output_bytes
- **Sensitive env masking** - API keys, passwords, tokens never returned raw
- **Timeout enforcement** - every subprocess call has a deadline
- **Audit log** - every call recorded with command, cwd, exit code, timestamp
- **Blocking patterns** - sleep >2s and sed -i rejected automatically
- **300ms stabilization check** - background tasks validated before returning task_id

---

## Background Task Lifecycle

```
Async launch (run_in_background=true)
  ↓
300ms stabilization check (detect immediate failures)
  ↓
If success: return task_id; process runs in background
If failure: return error immediately
  ↓
(while running) Poll with Status mode (task_id parameter)
  ↓
Process finishes naturally OR Kill mode terminates it
  ↓
Status mode returns final exit_code
  ↓
Auto-cleanup after 1 hour of being finished
```

---

## Platform Support

| Platform | Shell | Tested |
|----------|-------|--------|
| Linux | bash | Yes |
| macOS | bash | Yes |
| Windows | Git Bash (preferred) | Yes |
| Windows | PowerShell (via asyncio) | Yes |
| Windows | cmd.exe (via asyncio) | Yes |
