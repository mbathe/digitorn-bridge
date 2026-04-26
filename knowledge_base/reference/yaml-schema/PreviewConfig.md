---
id: yaml-schema-previewconfig
title: "PreviewConfig — YAML schema reference"
type: schema-reference
model: PreviewConfig
is_root: false
keywords: [previewconfig, command, cwd, enabled, env, health_path, install_command, port, restart_on_crash, startup_timeout]
---

# PreviewConfig

## Description
Dev server spawned on app deploy and proxied through the daemon.

Example YAML::

preview:
enabled: true
command: [npm, run, dev]
cwd: ./web
port: 5173
install_command: [npm, install]
health_path: /
env:
VITE_API_URL: "http://localhost:8000"

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `enabled` | bool |  | `True` | Disable to skip starting the preview server without removing the block. |
| `command` | list[str] | ✓ | — | Command + args to run, e.g. ['npm', 'run', 'dev']. |
| `cwd` | str |  | `'.'` | Working directory for the preview process, relative to the package bundle dir. |
| `port` | int | ✓ | — | Port the dev server binds to on localhost. |
| `env` | dict[str, str] |  | `{}` | Extra environment variables for the preview process. |
| `install_command` | list[str] \| null |  | `None` | Optional command to run once when the package is installed (e.g. ['npm', 'install']). Runs from ``cwd``. |
| `health_path` | str |  | `'/'` | HTTP path polled to detect dev-server readiness. |
| `startup_timeout` | float |  | `60.0` | Seconds to wait for the health check before declaring the preview failed. |
| `restart_on_crash` | bool |  | `True` | Restart the preview process if it exits unexpectedly (max 3 retries per minute). |

## Strictness
- `extra: forbid` — unknown keys cause a validation error
