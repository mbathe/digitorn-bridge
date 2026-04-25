---
id: shell-bash
title: "shell.bash (Bash)"
type: module-action
module: shell
action: bash
fqn: shell.bash
short_name: Bash
keywords: [shell, bash, exec, executer, commande, cmd, run]
permissions: [process.exec]
risk_level: high
irreversible: false
require_approval: false
---

# shell.bash (Bash)

## Description
Execute shell commands: sync, async, status check, or kill.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `command` | string |  | — | Shell command to execute. Use && to chain commands, ; for independent commands. Not needed if checking status via task_id. |
| `description` | string |  | `` | Short label shown in the UI (e.g. 'Running tests'). |
| `run_in_background` | boolean |  | `False` | Run in background (async). Returns task_id immediately. Useful for long-running tasks. |
| `task_id` | string |  | — | Task ID from previous run_in_background=true. Omit command to check status. |
| `kill` | boolean |  | `False` | Set true to kill a running task (requires task_id). |
| `stdin_text` | string |  | — | Text to send to running task's stdin (requires task_id). Newline appended automatically. |
| `wait` | boolean |  | `False` | Block until background task completes (requires task_id). Returns final result. |
| `stream` | boolean |  | `False` | Stream output with line-by-line notifications (like Monitor tool). |
| `stream_pattern` | string |  | — | Regex pattern to filter streamed lines. Only matching lines trigger notifications. |
| `timeout` | number |  | `300.0` | Max time to wait (seconds) |
| `cwd` | string |  | `.` | Working directory |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: shell
      actions: [bash]
```

## Tool usage instructions
```
Execute shell commands. Your interface to git, build tools, test runners, and system commands.

## Modes
Bash(command='ls -la') — sync, wait for result (default timeout: 300s)
Bash(command='npm start', run_in_background=true) — async, returns task_id
Bash(task_id='abc') — check background task status
Bash(task_id='abc', kill=true) — terminate background task
Bash(task_id='abc', stdin_text='y\n') — send input to interactive process

## When to use
- git operations: status, diff, add, commit, push, log, branch
- Build tools: make, npm run build, cargo build, pip install
- Test runners: pytest, npm test, cargo test, go test
- Package managers: npm install, pip install, cargo add
- System: ls, pwd, env, which, file, wc
- Long-running: dev servers, watchers, compilers → use run_in_background=true

## When NOT to use — use dedicated tools
- Read files → Read (NOT cat/head/tail)
- Edit files → Edit (NOT sed/awk)
- Create files → Write (NOT echo/cat)
- Search content → Grep (NOT grep/rg)
- Find files → Glob (NOT find/ls)

## Git safety rules
- ALWAYS git status before and after changes
- ALWAYS git diff to review before committing
- ALWAYS stage specific files (git add path/to/file) — NEVER git add -A or git add .
- ALWAYS create NEW commits — NEVER amend unless explicitly asked
- NEVER skip hooks (--no-verify) or force push (--force)
- NEVER push unless explicitly told to

## Behavior rules
- Check exit codes — non-zero means failure, read stderr
- If a command fails, diagnose the error before retrying
- For destructive commands (rm -rf, git reset), ask the user first
- Chain independent commands with && (stops on first failure)
- Long builds/tests → run_in_background=true, then check with task_id
- After implementing changes, ALWAYS run tests to verify
```

## Aliases
`executer`, `commande`, `cmd`, `run`

## Safety
- Required permissions: `process.exec`
- Risk level: **high**
