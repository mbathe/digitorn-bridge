---
id: yaml-schema-sandboxconfig
title: "SandboxConfig — YAML schema reference"
type: schema-reference
model: SandboxConfig
is_root: false
keywords: [sandboxconfig, allow_paths, audit, idle_timeout, level, namespaces, pool_max, pool_size, resources, session_timeout, workspace_snapshot]
---

# SandboxConfig

## Description
OS-level sandbox configuration for per-session isolation.

Levels (presets):
- off: no sandbox (current non-sandbox path)
- standard: Landlock + seccomp + cgroups (single worker)
- strict: + warm pool + user/PID namespaces + capability drop + MDWE
- maximum: + network namespace + seccomp-notify audit + workspace snapshot

Example::

execution:
sandbox:
level: strict
pool_size: 4
namespaces: [user, pid, net]

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `level` | 'off' \| 'standard' \| 'strict' \| 'maximum' |  | `'standard'` | Sandbox level preset: 'off', 'standard', 'strict', or 'maximum'. |
| `pool_size` | int |  | `2` | Number of pre-warmed workers in the pool. |
| `pool_max` | int |  | `8` | Maximum workers under load (pool_size ≤ pool_max). |
| `namespaces` | list[str] |  | `[]` | Linux namespaces to create: 'user', 'pid', 'net', 'mount'. |
| `workspace_snapshot` | bool |  | `False` | Enable CoW workspace snapshots per session. |
| `audit` | bool |  | `False` | Enable per-session audit trail (security event log). |
| `session_timeout` | int |  | `3600` | Maximum session duration in seconds before auto-termination. |
| `idle_timeout` | int |  | `300` | Idle timeout in seconds before worker recycling. |
| `allow_paths` | list[str] |  | `[]` | Additional filesystem paths the sandbox may access, beyond the workspace. Each entry is 'path' (read-only) or 'path:rw' (read-write). Supports {{variables}} and ~ for home directory. Example: ['/data/models', '~/datasets:rw', '/etc/myapp'] |
| `resources` | dict[str, any] |  | `{}` | Per-worker resource limits. Keys: 'memory' (e.g. '512MB'), 'cpu' (cores), 'processes' (max PIDs). |

## Strictness
- `extra: forbid` — unknown keys cause a validation error
