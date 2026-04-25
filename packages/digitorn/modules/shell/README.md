# Shell Module

1 ultra-powerful bash action with 7 execution modes + progressive notifications.

## Overview

The Shell module gives AI agents full control over system command execution.
Platform-adaptive: Linux/macOS (Bash) and Windows (Git Bash, PowerShell, cmd.exe) are handled transparently.

Matches Claude Code power level with streaming, waiting, and scheduling capabilities.

- **`Bash`** is the single action with 7 modes:
  1. **Sync mode**: execute normally, wait for result (default)
  2. **Async mode**: launch in background (run_in_background=true), return task_id immediately
  3. **Status mode**: check on a background task (task_id parameter)
  4. **Kill mode**: terminate a background task (task_id + kill=true)
  5. **Stdin mode**: send input to interactive background tasks (task_id + stdin_text)
  6. **Wait mode**: block until background task completes (task_id + wait=true)
  7. **Stream mode**: stream output line-by-line with pattern filtering (stream=true, stream_pattern=regex)

## Action

| Action | Description | Risk | Permissions |
|--------|-------------|------|-------------|
| `bash` | Execute shell commands (sync, async, status, kill, stdin) | High | `process.exec` |

## Usage

### Sync execution (default)
```
Bash(command='ls -la')
```
Executes a command and waits for result. Returns stdout, stderr, exit code, platform info.

### Async execution (background)
```
Bash(command='npm start', run_in_background=true)
```
Launches a long-running command, returns task_id immediately. Process runs in background.
**Notifications**: Agent receives periodic progress updates at T+5s, T+20s, T+50s, then every 60s.

### Check background task
```
Bash(task_id='abc123')
```
Check status of a running task. Returns status, output tail, uptime.

### Kill background task
```
Bash(task_id='abc123', kill=true)
```
Terminate a background task (graceful SIGTERM → forceful SIGKILL).

### Send input to interactive task
```
Bash(task_id='abc123', stdin_text='y\n')
```
Send text to a task's stdin. Useful for interactive prompts (y/n, passwords, etc.).

### Wait for background task completion
```
Bash(task_id='abc123', wait=true, timeout=300)
```
Block until background task completes, like waiting for pytest. Returns final stdout/stderr/exit_code.
Useful when agent needs to wait for a result before proceeding.

### Stream output with pattern filtering
```
Bash(command='npm build', stream=true, stream_pattern='error|warning')
```
Stream command output line-by-line with optional regex filtering. Like Monitor tool.
Each matching line triggers a notification. Useful for following long-running builds.
Only lines matching the pattern trigger notifications (no pattern = all lines).

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `command` | str | No | — | Shell command to execute (required for sync/async/stream) |
| `description` | str | No | "" | Label shown in UI |
| `run_in_background` | bool | No | False | Launch async (Async mode) |
| `task_id` | str | No | — | Task ID for status/kill/stdin/wait (hidden parameter) |
| `kill` | bool | No | False | Kill the task (hidden parameter) |
| `stdin_text` | str | No | — | Text to send to stdin (hidden parameter) |
| `wait` | bool | No | False | Block until task completes (hidden parameter) |
| `stream` | bool | No | False | Stream output with line-by-line notifications (hidden parameter) |
| `stream_pattern` | str | No | — | Regex to filter streamed lines (hidden parameter) |
| `timeout` | float | No | 30.0 | Max time to wait in seconds (hidden parameter) |
| `cwd` | str | No | "." | Working directory (hidden parameter) |

## Constraints

```yaml
modules:
  shell:
    constraints:
      max_output_bytes: 1000000  # Max stdout/stderr size
```

## Security

- **Command blacklist** — platform-specific patterns (fork bombs, disk wipes, etc.)
- **Workspace confinement** — cwd always resolved inside allowed root
- **Output size cap** — stdout/stderr truncated at configurable max_output_bytes
- **Sensitive env masking** — API keys, passwords, tokens never returned raw
- **Timeout enforcement** — every subprocess call has a deadline
- **Audit log** — every call recorded with command, cwd, exit code, timestamp
- **Blocking patterns** — sleep >2s and sed -i are blocked (use background mode or Edit action instead)
- **300ms stabilization check** — background tasks validated before returning task_id

## Platform Support

| Platform | Status | Shell |
|----------|--------|-------|
| Linux | Supported | bash |
| macOS | Supported | bash |
| Windows | Supported | bash (Git Bash), powershell, cmd.exe |

## How Agent Gets Notified

Background tasks send **progressive notifications** to the agent:

```
T+0s   → Bash() returns: {task_id: 'abc123', status: 'running', pid: 1234, platform: 'windows', shell: 'C:\...\bash.exe'}

T+5s   → [BACKGROUND TASK PROGRESS] task_id=abc123, elapsed=5s
         "Building... 3 new lines"
         
T+20s  → [BACKGROUND TASK PROGRESS] task_id=abc123, elapsed=20s
         "Compiling module foo..."
         
T+50s  → [BACKGROUND TASK COMPLETED] task_id=abc123, elapsed=50s
         Result: "Build successful!\nTests passed\n..."
         Exit code: 0.
```

The agent is **automatically woken up** to process these notifications (via `manager.check_notifications()`).

## Requirements

No external dependencies. Uses only Python standard library (`asyncio`, `subprocess`, `shutil`, `pathlib`).

## Background Task Management

Background tasks are tracked per-session with:
- Async subprocess execution (truly non-blocking)
- Real-time output streaming (deque-based buffering, max 10,000 lines per stream)
- Automatic cleanup of finished tasks (after 1 hour)
- Graceful termination (SIGTERM then SIGKILL)
- Progressive notifications (not just one-shot at completion)
- Platform info included in every result

## Platform Detection

The agent always knows its environment through:
1. **System prompt** — `Execution Environment` section with OS, shell, workspace, path separator, temp dir, and platform-specific syntax examples
2. **Tool results** — Every bash result includes `platform` ("unix" | "windows") and `shell` (executable path)
