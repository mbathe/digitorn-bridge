---
id: shell
title: shell Module
sidebar_label: shell
sidebar_position: 2
description: One Bash tool with five modes - sync, async, status, kill, stdin/wait/stream. Git Bash on Windows.
---

# shell

A single `Bash` tool with five operating modes dispatched via
params. The agent sees one tool with minimal visible params;
modes are picked from `run_in_background`, `task_id`, `kill`,
`stdin_text`, `wait`, `stream`.

| Property | Value |
|----------|-------|
| Module id | `shell` |
| Version | `1.0.0` |
| Action count | 1 (`shell.bash` → tool name `Bash`) |
| Type | user (per-app instance, per-session task tracking) |
| Pip deps | none (uses stdlib `subprocess`). |
| Platforms | Linux, macOS, Windows (Git Bash). |

## The single `Bash` action - 5 modes

Visible params:

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `command` | string | required (sync) | Shell command. Use `&&` to chain, `;` for independent. Omit when polling via `task_id`. |
| `description` | string | `""` | Short label shown in UI (e.g. `"Running tests"`). |
| `run_in_background` | bool | `false` | Return immediately with `task_id` instead of waiting. |

Hidden params (filtered from the LLM schema):

| Param | Type | Default | Purpose |
|-------|------|---------|---------|
| `task_id` | string | `null` | Reference a prior background task. |
| `kill` | bool | `false` | Terminate a running task (requires `task_id`). |
| `stdin_text` | string | `null` | Append + send to a running task's stdin (newline appended). |
| `wait` | bool | `false` | Block until a background task exits. |
| `stream` | bool | `false` | Push matching lines as notifications. |
| `stream_pattern` | string | `null` | Regex filter for streamed lines. |
| `timeout` | float | `300` | Max seconds, range `[1, 1800]`. |
| `cwd` | string | `.` | Working directory. |

### Mode 1 - Sync

```python
Bash(command="pytest -v")
# returns: {stdout, stderr, exit_code, duration, cwd}
```

### Mode 2 - Async (background)

```python
Bash(command="npm run build", run_in_background=true)
# returns: {task_id, pid, started_at}
```

Subsequent turns can poll, wait, kill, or stream.

### Mode 3 - Status

```python
Bash(task_id="...")          # no command
# returns: {stdout, stderr, exit_code (null if running), uptime_seconds, is_running}
```

### Mode 4 - Kill

```
Bash(task_id="...", kill=true)
```

Sends `SIGTERM`; escalates to `SIGKILL` after 2 s if still
alive.

### Mode 5 - Stdin / Wait / Stream

```
Bash(task_id="...", stdin_text="yes")
Bash(task_id="...", wait=true)
Bash(task_id="...", stream=true, stream_pattern="ERROR|WARN")
```

## Output structure

Every call returns:

```json
{
  "stdout": "...",
  "stderr": "...",
  "exit_code": 0,
  "duration": 1.42,
  "cwd": "/path/where/it/ran",
  "command": "the original command"
}
```

Output is redacted before being returned to the agent - API
keys, tokens, JWTs are replaced with `[REDACTED]`. Add custom
patterns via `extra_sensitive_patterns`.

## Constraints

 Four constraints:

| Constraint | Type | Scope | Description |
|------------|------|-------|-------------|
| `allowed_commands` | `string_list` | universal | Allowlist for the executable name (first token). |
| `blocked_commands` | `string_list` | universal | Blocklist (always applied on top of platform defaults). |
| `allowed_paths` | `string_list` | module | Extra directories beyond `runtime.workdir`. |
| `unrestricted` | `boolean` | module | Disable path confinement entirely (default `false`). |

## Configuration

```yaml
tools:
  modules:
    shell:
      config:
        shell: null                       # null = auto (Git Bash on Windows, /bin/bash elsewhere)
        timeout_default: 300
        max_output_lines: 10000           # per background task
        extra_sensitive_patterns: []      # additional regex for output redaction
        allowed_roots:                    # workspace + $HOME + tmpdir always allowed
          - "{{workdir}}"
          - "/tmp"
        blocked_commands:                 # appended to platform defaults
          - "shutdown"
          - "reboot"
      constraints:
        readonly: false                   # when true, block rm/mv/cp/mkdir/chmod/chown/...
```

## Path confinement

Allowed roots: `runtime.workdir` + `$HOME` + system tmpdir +
every `allowed_roots` entry. Anywhere else is rejected with a
clear error including the constraint to add.

On Windows, Git Bash paths (`/c/Users/...`) are auto-converted
to Windows form (`C:/Users/...`) before the check.

## Cleanup

`cleanup_session(session_id)` runs automatically when a
session aborts or ends. It kills every background task,
sends `SIGTERM` then `SIGKILL` after 2 s, and emits
cancellation notifications. Wired into the abort flow at
`abort_session`.

## Windows notes

- Executor finds **Git Bash** via an explicit path lookup -
  **NEVER** `shutil.which("bash")` (returns WSL bash, which
  crashes).
- Bash syntax (`&&`, `|`, `2>&1`, `grep`, `cat`, `head`,
  `tail`) runs natively. No PowerShell conversion layer.
-  does
  the search.

## Audit logging

Every `Bash` call logs: `command`, `cwd`, `exit_code`,
`error`, `timestamp`. Grep `shell_audit` in the daemon logs
to reconstruct activity.

## Cross-references

- App-config block reference (`tools.modules.shell`):
  [App Configuration → tools.modules](../../language/02-app-config.md#toolsmodules---module-configuration)
- OS-level sandbox (seccomp blocks `execve` if `shell` isn't
  loaded): [OS-Level Sandbox → seccomp](../../language/35-sandbox.md#2-seccomp-bpf---syscalls)
- Behaviour engine (built-in `confirm_destructive` rule blocks
  `rm -rf`, `git reset --hard`, ...):
  [Behavior Engine](../../language/43-behavior.md#prohibition-rules)
