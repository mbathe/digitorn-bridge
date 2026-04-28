---
id: shell
title: Shell Module
sidebar_label: shell
sidebar_position: 2
description: 1 ultra-powerful bash action with 5 modes - sync, async, status, kill, stdin, stream.
---

# shell

Execute shell commands with full output capture, background task management, and stdin interaction.

**One action, five modes.** The agent sees a single `Bash` tool with minimal visible params; modes are dispatched from `run_in_background`, `task_id`, `kill`, `stdin_text`, `wait`, and `stream`.

| Property | Value |
|----------|-------|
| **Module ID** | `shell` |
| **Isolation** | shared (per-app); tasks tracked per-session |
| **Platforms** | Linux, macOS, Windows (Git Bash) |
| **Dependencies** | None (uses subprocess) |

---

## Design Philosophy

- **One tool to rule them all** - LLMs handle a single `Bash` tool better than 10 specialized ones. Modes dispatch via params.
- **Structured output** - stdout, stderr, exit code, duration, cwd returned as separate fields.
- **Background tasks** - long-running commands return a `task_id` immediately and report progress via notifications.
- **Security-first** - platform-specific command blacklist, workspace path confinement, output sanitization (redacts API keys/tokens), timeout enforced, audit log on every call.
- **Windows native** - uses Git Bash (not WSL, not PowerShell). `&&`, pipes, `grep`, `cat`, `2>&1` all work.

---

## The `Bash` action - 5 modes

| Tool Name | Action |
|-----------|--------|
| `Bash` | `shell.bash` |

**Visible params** (always shown in the LLM schema):

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `command` | string | - | Shell command. Use `&&` to chain, `;` for independent. Omit when checking status via `task_id`. |
| `description` | string | `""` | Short label shown in UI (e.g. `"Running tests"`). |
| `run_in_background` | bool | `false` | Return immediately with `task_id` instead of waiting. |

**Hidden params** (mode selectors and execution knobs):

| Param | Type | Default | Purpose |
|-------|------|---------|---------|
| `task_id` | string | null | Reference a prior background task. |
| `kill` | bool | false | Terminate a running task (requires `task_id`). |
| `stdin_text` | string | null | Send text to a running task's stdin. |
| `wait` | bool | false | Block until background task completes. |
| `stream` | bool | false | Stream lines as notifications. |
| `stream_pattern` | string | null | Regex filter for streamed lines. |
| `timeout` | float | 300 | Max seconds (1–1800). |
| `cwd` | string | `.` | Working directory. |

---

### Mode 1 - Sync

`Bash(command="pytest -v")` → wait for completion, return stdout/stderr/exit_code.

### Mode 2 - Async (background)

`Bash(command="npm run build", run_in_background=true)` → returns `{task_id: "...", pid, started_at}` immediately. Subsequent turns can poll, wait, or kill.

### Mode 3 - Status

`Bash(task_id="...")` - no `command`. Returns current stdout/stderr buffers, `exit_code` (null if still running), `uptime_seconds`, `is_running`.

### Mode 4 - Kill

`Bash(task_id="...", kill=true)` - sends SIGTERM then SIGKILL if still alive after 2 s.

### Mode 5 - Stdin / Wait / Stream

- `Bash(task_id="...", stdin_text="yes")` - append newline, send to the task's stdin.
- `Bash(task_id="...", wait=true)` - block until the task exits, return final result.
- `Bash(task_id="...", stream=true, stream_pattern="ERROR|WARN")` - push matching lines as notifications until the task exits.

---

## Cleanup

`cleanup_session(session_id)` is called automatically when a session is aborted or ends - kills all background tasks and emits cancellation notifications.

---

## YAML configuration

```yaml
modules:
  shell:
    config:
      shell: null                    # null = auto (Git Bash on Windows, /bin/bash elsewhere)
      timeout_default: 300
      max_output_lines: 10000        # per background task
      extra_sensitive_patterns: []   # additional regex for output redaction
      allowed_roots:                 # path allow-list (workspace + HOME + temp always allowed)
        - "{{workspace}}"
        - "/tmp"
      blocked_commands:              # additional blacklist (platform default already applied)
        - "shutdown"
        - "reboot"
    constraints:
      readonly: false                # when true, block write-ish commands (rm, mv, cp, mkdir, chmod, ...)
```
---

## Windows notes

- Executor uses **Git Bash** via an explicit path lookup (NEVER `shutil.which("bash")` - that returns WSL bash, which crashes).
- Git Bash paths (`/c/Users/...`) are auto-converted to Windows paths (`C:/Users/...`) before workspace checks.
- No PowerShell conversion - bash syntax (`&&`, `|`, `2>&1`, `grep`, `cat`, `head`, `tail`) runs natively.

## Audit logging

Every `Bash` call logs: command, cwd, exit code, error, timestamp. Grep `shell_audit` in the daemon logs to reconstruct activity.
