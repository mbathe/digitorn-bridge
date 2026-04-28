---
id: module-concept-shell
title: "shell module - overview"
type: module-concept
module: shell
isolation: shared
keywords: [shell, shell-module, bash]
version: 1.0.0
---

# `shell` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `1.0.0`
- **Actions**: 1 visible, 0 internal

## Description (from class docstring)

Shell module - 1 ultra-powerful bash action with 4 execution modes.

bash - execute shell commands with 4 modes:
  1. Sync execution: normal commands (wait for result)
  2. Async execution: long-running commands (return task_id immediately)
  3. Status checking: check running task status and output
  4. Task management: kill running tasks

Security (all platforms):
  - Platform-specific command blacklist checked before every execution.
  - Workspace path confinement: absolute paths outside workspace are blocked.
  - Output sanitization: API keys, passwords, tokens are redacted.
  - Timeout enforced on every subprocess call.
  - Audit log: every command recorded with command, cwd, exit code, timestamp.

## Configuration

Set under `modules.shell.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon at module init time. Do NOT set manually in YAML - the daemon resolves it from the app's workspace/workspace_mode config. |
| `timeout` | int |  | `300` | Default timeout (seconds) |
| `max_output_bytes` | int |  | `1000000` | Max output size |
| `sanitize_output` | bool |  | `True` | Redact secrets from output |
| `persist_large_output` | bool |  | `True` | Save large output (>30KB) to disk |
| `large_output_threshold` | int |  | `30000` | Bytes threshold for disk persistence |
| `max_persisted_bytes` | int |  | `64000000` | Max bytes to persist to disk (64MB) |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `bash` | `Bash` |  | high | Execute shell commands: sync, async, status check, or kill. |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: shell
      actions: [bash]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {shell: [bash]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/shell-*.md`.
