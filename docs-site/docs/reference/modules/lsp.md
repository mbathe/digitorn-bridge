---
id: lsp
title: lsp Module
sidebar_label: lsp
description: Universal real-time language feedback - LSP servers, compilers, linters. Auto-detect, lazy startup, built-in fallback parsers.
---

# lsp

The **lsp** module is Digitorn's universal real-time feedback
channel for any language. Every entry in its YAML config
becomes a persistent feedback channel running under one of
three protocols: **LSP** (JSON-RPC persistent - pyright,
gopls, texlab, rust-analyzer), **compiler** (re-run after
each edit - `cargo check`, `tsc --noEmit`), or **linter**
(shell-out on demand - ruff, eslint, stylelint).

| Property | Value |
|----------|-------|
| Module id | `lsp` |
| Version | `3.0.0` |
| Action count | 5 (all internal) |
| Type | system (called by `workspace`, `filesystem`, agents via the REST `/lsp/*` endpoints) |

## The 5 internal actions

Every action is internal - agents don't call them directly.
The workspace + filesystem modules call them via injected
references; the daemon's REST `/api/apps/{id}/sessions/{sid}/lsp/*`
routes call them for IDE-style integrations.

| Action | Purpose |
|--------|---------|
| `lsp.diagnostics` | Get errors / warnings for a file or the whole project. |
| `lsp.check` | Quick pass / fail for one file (`{passed: bool}`). |
| `lsp.notify_change` | Trigger fresh diagnostics after an edit (LSP: push `didChange`; compiler / linter: re-run). |
| `lsp.request` | Forward a raw LSP request (hover / goto / references / completion / rename / ...) to the language server backing a file. |
| `lsp.cancel_request` | Cancel an in-flight LSP request by `request_id`. |

## Protocol modes

Auto-detected from the command name:

| Mode | Triggers | Behaviour |
|------|----------|-----------|
| `lsp` | `*langserver`, `*-language-server`, `gopls`, `pyright`, `pylsp`, `texlab`, `rust-analyzer`, `vscode-*` | Long-running JSON-RPC subprocess, push diagnostics on `didChange`. |
| `compiler` | `cargo check`, `go vet`, `tsc --noEmit`, anything with `check` / `build` / `compile` / `noemit` / `watch` | Re-run `command` after each notified change, parse stdout. |
| `linter` | `ruff`, `eslint`, `stylelint`, `flake8`, `pylint`, `mypy`, `black`, `prettier`, `biome` (or fallback) | Shell-out per file, parse output. |

Parser is auto-detected the same way (`ruff`, `eslint`,
`tsc`, `cargo`, `govet`, or `fallback`).

## Configuration

### Minimal - auto-detect

```yaml
tools:
  modules:
    lsp: {}
```

Empty config triggers a workspace scan for marker files. The
matching servers are registered as **pending** - they start
lazily on first use.

### Simple - one entry per language

```yaml
tools:
  modules:
    lsp:
      config:
        python: "pyright-langserver --stdio"
        rust: "cargo check --message-format=json"
        latex: "texlab"
```

Protocol + extensions + parser are all auto-derived from the
command name and the language key (looked up in
`_NAME_TO_EXTENSIONS`).

### Full control

```yaml
tools:
  modules:
    lsp:
      config:
        servers:
          python:
            command: "pyright-langserver --stdio"
            protocol: lsp
            extensions: [.py, .pyi]
            parser: fallback
          latex:
            command: "texlab"
            protocol: lsp
            extensions: [.tex, .bib]
          css:
            command: "stylelint --formatter=json"
            protocol: linter
            extensions: [.css, .scss]
            parser: fallback
```

## Constraints

The LSP module declares **only the universal action-level
constraints** that every Digitorn module supports. There is **no
server-level whitelist constraint** (no `enabled_servers`, no
`disabled_servers`).

| Constraint        | Type          | Scope     | Purpose                                                                      |
|-------------------|---------------|-----------|------------------------------------------------------------------------------|
| `allowed_actions` | `string_list` | universal | Restrict which `lsp.*` actions the agent can call (e.g. only `diagnostics`). |
| `blocked_actions` | `string_list` | universal | Block specific actions (e.g. `request`).                                     |

To restrict which **servers** ever spawn for an app, do it through
`config:` — the LSP module uses lazy on-demand startup, so a server
that isn't configured never runs. See the recipe below.

## Recipe: restrict to one stack (JS/TS only)

A React-builder app that only deals with TypeScript / JavaScript
doesn't need pyright / gopls / rust-analyzer eating subprocess
slots. Just configure the JS/TS toolchain and nothing else:

```yaml
tools:
  modules:
    lsp:
      config:
        typescript: "typescript-language-server --stdio"
        tsc: "tsc --noEmit --pretty false"
        eslint: "eslint --format=json"
```

In this app, opening a `.py` / `.go` / `.rs` file does **not**
start the corresponding LSP — those languages aren't in `config:`,
so the registry lookup returns "no server configured" and the
action returns cleanly. No spawn, no waste, no error.

## Auto-detect markers

Used when `lsp: {}`:

| Language | Command | Markers |
|----------|---------|---------|
| python | `pyright-langserver --stdio` | `pyproject.toml`, `setup.py`, `requirements.txt`, any `.py` |
| typescript | `typescript-language-server --stdio` | `tsconfig.json`, `package.json` |
| go | `gopls` | `go.mod` |
| rust | `rust-analyzer` | `Cargo.toml` |
| latex | `texlab` | any `.tex` |
| css | `vscode-css-language-server --stdio` | any `.css`, `.scss` |
| html | `vscode-html-language-server --stdio` | any `.html` |
| json | `vscode-json-language-server --stdio` | any `.json` |

If the LSP binary isn't on PATH, the module falls back to a
matching linter from `_FALLBACK_LINTERS` (ruff for Python,
eslint for TS / JS, `tsc --noEmit`, `cargo check`,
`go vet -json`).

## Diagnostics return shape

```json
{
  "mode": "lsp|compiler|linter",
  "server": "python",
  "path": "src/auth.py",
  "diagnostics": [
    {
      "severity": "error|warning|info|hint",
      "line": 42, "column": 11,
      "message": "Undefined name 'foo'",
      "code": "F821", "source": "ruff"
    }
  ],
  "total": 5, "errors": 2, "warnings": 3
}
```

Diagnostics are **capped** to keep LLM context bounded:
50 / call (`diagnostics`), 100 / call (`notify_change`),
20 / call (`check`).

## `notify_change` flow

1. Resolve protocol for the file's extension (start pending
   spec if needed).
2. Call `proto.notify_file_changed(path, content)`.
3. Sleep 0.3 s for LSP mode (time for server push); 0.0 s
   for compiler / linter.
4. Collect diagnostics and return.

Called **automatically** via tool hooks after every
`filesystem.write`, `filesystem.edit`, `workspace.write`,
`workspace.edit` - so the agent doesn't normally need to
call it by hand.

## Built-in fallback validators

`modules/lsp/parsers.py`. When no LSP server is configured
or available, the workspace + filesystem modules fall back
to in-memory parsers - no external tools needed:

| Format | Extensions | Checks |
|--------|------------|--------|
| JSON | `.json`, `.jsonc` | Structural errors with line / col. |
| YAML | `.yaml`, `.yml` | `yaml.safe_load` errors. |
| TOML | `.toml` | Parse errors. |
| Python | `.py`, `.pyi` | `ast.parse` syntax errors. |
| LaTeX | `.tex` | Unmatched braces + unclosed `\begin{...}\end{...}`. |

Resolution order inside `workspace` / `filesystem`:

1. Real LSP server (when loaded and running).
2. Built-in validator (in-memory, zero external deps).
3. No lint info.

## Integration - `workspace` + `filesystem`

Both modules receive an injected `self._lsp` reference at
bootstrap. When `lint: true` (default for `workspace`),
every `write` and `edit`:

1. Runs the write / edit.
2. Calls `lsp.notify_change(path, content)` in a try / except.
3. Embeds the returned diagnostics as a `lint` field in the
   tool response.

```json
{
  "success": true,
  "path": "src/App.tsx",
  "lint": {
    "mode": "lsp", "server": "typescript",
    "errors": 1, "warnings": 0,
    "diagnostics": [{ "line": 12, "message": "Cannot find name 'Footer'" }]
  }
}
```

The agent sees failures inline and can fix them immediately.
No separate `diagnostics` call required.

## Lifecycle

| Hook | Behaviour |
|------|-----------|
| `on_config_update(cfg)` | Parses YAML, starts explicit servers, registers markers for auto-detected ones as pending. |
| `_get_protocol(path)` | Resolves ext → protocol; lazily starts pending spec on first use; falls back to linter if LSP binary missing. |
| `on_stop` | Stops all protocol instances; shuts down sidecar pool if owned. |

Servers run inside the daemon's shared `DaemonSidecarPool` -
one pool per daemon, not per app. If an app configures LSP
before the pool exists, the module creates and owns its own
pool (`_owns_pool = True`).

## Integration notes

- **Not Socket.IO** - diagnostics are returned inline in
  tool responses; this module doesn't publish events.
  Real-time UI updates flow through `workspace` →
  `preview` (the `lint` field on the file payload).
- **Lazy startup** - auto-detected servers don't eat memory
  until the first relevant file is written. Explicit config
  starts servers eagerly.
- **REST endpoints** - the daemon exposes a per-session LSP
  surface (raw RPC pass-through + cancel) for IDE-style
  integrations. The route shapes are not documented publicly.

## Cross-references

- App-config block reference (`tools.modules.lsp`):
  [App Configuration → tools.modules](../../language/02-app-config.md#toolsmodules---module-configuration)
- Workspace module (calls `lint: true` automatically):
  [workspace reference](workspace.md)
- Filesystem module (built-in validators apply on write /
  edit when LSP module isn't loaded):
  [filesystem reference](filesystem.md)
- LSP REST endpoints:
  [API Integration → LSP](../../language/14-api-integration.md)
