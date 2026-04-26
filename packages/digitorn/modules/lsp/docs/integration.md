# lsp — Integration Guide

`lsp` bridges the agent to real **Language Server Protocol** processes
(pyright, ruff, eslint, texlab, ...) so the agent can get the same
diagnostics and completions your IDE gets.

## Actions

| Action | Purpose |
|---|---|
| `diagnostics` | Get errors/warnings for a file or a whole project. |
| `check` | Quick pass/fail check on a single file. |
| `notify_change` | Tell the LSP server a file just changed (hot path for post-write validation). |
| `request` | Generic LSP RPC (`textDocument/hover`, `definition`, `completion`, `rename`, ...). |
| `cancel` | Cancel an in-flight RPC by request id. |

## How language detection works

The module ships a **server registry** in `module.py` keyed on file
extensions:

```python
{"name": "python", "command": "pyright-langserver --stdio",
 "markers": ["pyproject.toml", "setup.py", "requirements.txt", ".py"]},
{"name": "typescript", "command": "typescript-language-server --stdio",
 "markers": [".ts", ".tsx", "tsconfig.json"]},
# ... eslint, ruff, texlab, gopls, etc.
```

On first call, the module:
1. Picks the right server from the file extension / project markers.
2. Checks the binary is in `PATH`; if not, the action returns a clean
   "server not installed" error instead of hanging.
3. Spawns the LSP process, initialises it with the workspace root, and
   caches the connection for the lifetime of the daemon.

## The typical post-write loop

```
agent → filesystem.write(path, content)
        │
        ▼
hook (lsp_diagnose action in hooks.yaml)
        │
        ▼
lsp.notify_change(path) → lsp.diagnostics(path)
        │
        ▼
errors injected back into the next agent turn via hook `inject_result`
```

This is how the self-correction loop works without the agent having to
call `lsp.check` explicitly every time.

## Constraints

| Constraint | Type | Scope | Purpose |
|---|---|---|---|
| `enabled_servers` | `string_list` | module | Whitelist which LSP servers this app may spawn (e.g. only `pyright`). |
| `disabled_servers` | `string_list` | module | Blacklist — useful to turn off `eslint` on a Python project. |

## Isolation

LSP processes are **shared across sessions of the same app** (the
initialisation cost is ~200 ms per server). Sub-agents reuse the same
pool. Workspace scope is per-app.

## When NOT to use

- You don't need LSP diagnostics. Built-in validators already cover
  JSON / YAML / TOML / Python syntax via `lsp/parsers.py`, and the
  filesystem module calls those validators as a fallback when no real
  LSP server is installed.
- You only want linting (not LSP semantics). Prefer the built-in
  validator path — less moving parts.

## Related

- `packages/digitorn/modules/lsp/parsers.py` — built-in fallback validators
- `docs/hooks.md` — the `lsp_diagnose` hook action that wires this
  module into the post-write loop automatically
