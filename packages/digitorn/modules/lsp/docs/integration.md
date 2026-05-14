# lsp - Integration Guide

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

The LSP module declares **only the universal constraints** that every
Digitorn module supports:

| Constraint | Type | Scope | Purpose |
|---|---|---|---|
| `allowed_actions` | `string_list` | universal | Restrict which actions of `lsp` the agent can call (e.g. only `diagnostics`). |
| `blocked_actions` | `string_list` | universal | Block specific actions (e.g. `analyze`). |

> **Server-level whitelisting**: there is **no** `enabled_servers` /
> `disabled_servers` constraint. The LSP module uses **lazy
> on-demand startup** — a server only spawns when a file of its
> language is first accessed AND that language is declared in
> `config:`. To restrict which servers ever run, just configure
> only the languages you want. See the "Restrict to JS/TS only"
> example below.

## Restrict to a specific stack — by config, not constraints

To pin the module to one language toolchain (e.g. JS/TS only for a
React-builder app), just declare what you want under `config:`. The
lazy startup guarantees nothing else ever spawns:

```yaml
tools:
  modules:
    lsp:
      config:
        typescript: "typescript-language-server --stdio"
        tsc: "tsc --noEmit --pretty false"
        eslint: "eslint --format=json"
```

In this app, opening a `.py` or `.go` file does **not** start
pyright / gopls — they aren't in `config:`, so the registry lookup
returns "no server configured for this language" and the action
returns cleanly. No subprocess overhead, no error to the agent.

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
  validator path - less moving parts.

## Related

- `packages/digitorn/modules/lsp/parsers.py` - built-in fallback validators
- `docs/hooks.md` - the `lsp_diagnose` hook action that wires this
  module into the post-write loop automatically
