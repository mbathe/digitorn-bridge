---
id: lsp
title: LSP Module
sidebar_label: lsp
description: Universal real-time language feedback - LSP servers, compilers, linters. Auto-detects project language; built-in fallback parsers for JSON, YAML, TOML, Python, LaTeX.
---

# lsp

The **lsp** module is Digitorn's universal real-time feedback channel for any
language. Every entry in its YAML config becomes a persistent feedback channel
running under one of three protocols: **LSP** (JSON-RPC persistent - pyright,
gopls, texlab, rust-analyzer), **compiler** (re-run after each edit - `cargo
check`, `tsc --noEmit`), or **linter** (shell-out on demand - ruff, eslint,
stylelint).

| Property | Value |
|----------|-------|
| **Module ID** | `lsp` |
| **Version** | `3.0.0` |
| **Platform** | All |
| **Actions exposed to LLM** | 3 |
| **Called by** | `workspace` (when `lint: true`), `filesystem`, agents |

---

## Protocol modes

| Mode | Use for | Behavior |
|------|---------|----------|
| `lsp` | Persistent language servers | Long-running JSON-RPC subprocess, push diagnostics on `didChange` |
| `compiler` | Type-checkers & build tools | Re-run `command` after each notified change, parse stdout |
| `linter` | On-demand shell linters | Invoke `command` per file, parse output |

The protocol is **auto-detected from the command name**:

- `*langserver`, `*-language-server`, `gopls`, `pyright`, `pylsp`, `texlab`,
  `rust-analyzer`, `vscode-*` → `lsp`
- `cargo check`, `go vet`, `tsc --noEmit`, anything with `check`/`build`/
  `compile`/`noemit`/`watch` → `compiler`
- `ruff`, `eslint`, `stylelint`, `flake8`, `pylint`, `mypy`, `black`,
  `prettier`, `biome` → `linter`
- Fallback → `linter`

Parser is auto-detected the same way (`ruff`, `eslint`, `tsc`, `cargo`,
`govet`, or `fallback`).

---

## Configuration

### Minimal - auto-detect

```yaml
modules:
  lsp: {}
```
With empty config, the module scans the workspace for marker files
(`pyproject.toml`, `tsconfig.json`, `go.mod`, `Cargo.toml`, `.tex`, etc.)
and registers the matching servers as **pending** - they start lazily on
first use.

### Simple - one entry per language

```yaml
modules:
  lsp:
    config:
      python: "pyright-langserver --stdio"
      rust: "cargo check --message-format=json"
      latex: "texlab"
```
Protocol + extensions + parser are all auto-derived from the command name
and the language key (looked up in `_NAME_TO_EXTENSIONS`).

### Full control

```yaml
modules:
  lsp:
    config:
      servers:
        python:
          command: "pyright-langserver --stdio"
          protocol: lsp
          extensions: [".py", ".pyi"]
          parser: fallback
        latex:
          command: "texlab"
          protocol: lsp
          extensions: [".tex", ".bib"]
        css:
          command: "stylelint --formatter=json"
          protocol: linter
          extensions: [".css", ".scss"]
          parser: fallback
```
### Auto-detect markers (used when `lsp: {}`)

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

If the LSP binary isn't on PATH, the module falls back to a matching linter
from `_FALLBACK_LINTERS` (ruff for Python, eslint for TS/JS, `tsc --noEmit`,
`cargo check`, `go vet -json`).

---

## Actions (3)

| Action | Visible params | Purpose |
|--------|---------------|---------|
| `diagnostics` | `path?: str` | Get errors/warnings for a file; if `path` omitted, list active + pending servers |
| `check` | `path: str` | Quick pass/fail for one file (`passed: bool`) |
| `notify_change` | `path: str`, `content?: str` | Trigger fresh diagnostics after an edit (LSP: push `didChange`; compiler/linter: re-run) |

Aliases recognized by the discovery layer:

- `diagnostics` → `lint`, `check_code`, `verifier`, `diagnostiquer`
- `check` → `verifier_fichier`, `lint_file`

### Return shape

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

Diagnostics are **capped** at 50 entries per call (100 for `notify_change`,
20 for `check`) to keep the LLM context bounded.

### `notify_change` flow

1. Resolve the protocol for the file's extension (starts pending spec if needed).
2. Call `proto.notify_file_changed(path, content)`.
3. Sleep `0.3s` for LSP mode (time for server push); `0.0s` for compiler/linter.
4. Collect diagnostics and return.

Called **automatically** via tool hooks after every `filesystem.write`,
`filesystem.edit`, `workspace.write`, `workspace.edit` - so the agent doesn't
normally need to call it by hand.

---

## Built-in fallback validators

When no LSP server is configured (or available), the **workspace** and
**filesystem** modules fall back to built-in in-memory parsers in
`modules/lsp/parsers.py`. These require no external tools:

| Format | Extensions | Checks |
|--------|-----------|--------|
| JSON | `.json`, `.jsonc` | Structural errors with line/col |
| YAML | `.yaml`, `.yml` | `yaml.safe_load` errors |
| TOML | `.toml` | Parse errors |
| Python | `.py`, `.pyi` | `ast.parse` syntax errors |
| LaTeX | `.tex` | Unmatched braces + unclosed `\begin{...}\end{...}` blocks |

Resolution order (inside workspace/filesystem):

1. Real LSP server (if loaded and running)
2. Built-in validator (in-memory, zero external deps)
3. No lint info

---

## Integration - `workspace` + `filesystem`

Both modules receive an injected `self._lsp` reference at bootstrap. When
`lint: true` (the default for `workspace`), every `write` and `edit` call:

1. Runs the write/edit.
2. Calls `lsp.notify_change(path, content)` in a try/except.
3. Embeds the returned diagnostics as a `lint` field in the tool response.

Result:

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

The agent sees failures inline and can fix them immediately - no separate
`diagnostics()` call required.

---

## Lifecycle

| Hook | Behavior |
|------|----------|
| `on_config_update(cfg)` | Parses YAML, starts explicit servers, registers markers for auto-detected ones as pending |
| `_get_protocol(path)` | Resolves ext → protocol; lazily starts pending spec on first use; falls back to linter if LSP binary missing |
| `on_stop()` | Stops all protocol instances; shuts down sidecar pool if owned |

Servers run inside the daemon's shared `DaemonSidecarPool` - one pool per
daemon, not per app. If an app configures LSP before the pool exists, the
module creates and owns its own pool (set `_owns_pool = True`).

---

## Integration notes

- **Not Socket.IO.** Diagnostics are returned inline in tool responses; this
  module does not publish Socket.IO events. Real-time UI updates flow through
  `workspace` → `preview` (the `lint` field on the file payload).
- **No workbench.** Diagnostics attach to files via the preview module, not
  a separate workbench surface.
- **Lazy startup.** Auto-detected servers don't eat memory until the first
  relevant file is written. Explicit config starts servers eagerly.

---

## Related

- [`workspace`](./workspace.md) - caller for every write/edit (`lint: true`)
- [`filesystem`](./filesystem.md) - caller for every write/edit on real disk
- `modules/lsp/parsers.py` - built-in fallback validators
- `modules/lsp/protocols.py` - LSP/compiler/linter protocol implementations
