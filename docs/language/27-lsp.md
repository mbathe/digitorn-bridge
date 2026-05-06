---
id: 27-lsp
---

# LSP — Real-Time Code Diagnostics

The `lsp` module
(`packages/digitorn/modules/lsp/module.py` `MODULE_ID = "lsp"`)
runs language servers (pyright, gopls, eslint, ruff, texlab, ...)
as persistent subprocesses and exposes their diagnostics to the
runtime. **All actions are internal** (`internal=True` on each
`@action`) — agents never call them directly. Diagnostics flow to
the agent through:

1. The inline `lint` field in write/edit responses (auto-attached
   by hooks like `lsp_diagnose`).
2. The `diagnostics` preview channel for the client UI.

Every action and field on this page maps to real code; entries
are cited with file + line.

## Module declaration

```yaml
tools:
  modules:
    lsp:
      config:
        # Per-extension server commands (auto-detection runs when
        # config is empty; see Auto-detection below).
        py:  "pyright-langserver --stdio"
        ts:  "typescript-language-server --stdio"
        tsx: "typescript-language-server --stdio"
        go:  "gopls"
        rs:  "rust-analyzer"
        tex: "texlab"
        # Built-in fallback validators run on top of LSP for these
        # formats and don't need any config:
        #   .json / .jsonc, .yaml / .yml, .toml, .py
```

The full `ModuleBlock` shape is in
[App Configuration → tools.modules](02-app-config.md#toolsmodules--module-configuration).

## The 5 internal actions

`module.py`. All declared with
`internal=True` so they're never injected into the agent's tool
index.

| Action | Source | Purpose |
|--------|--------|---------|
| `lsp.diagnostics` | `module.py` | Get diagnostics for a file or list active servers when called without `path`. |
| `lsp.check` | `module.py` | Quick pass/fail for a single file. |
| `lsp.notify_change` | `module.py` | Notify that a file was changed; triggers fresh diagnostics. Called by hooks and the workspace/filesystem modules after every write. |
| `lsp.request` | `module.py` | Generic LSP RPC request (hover, definition, references, ...). |
| `lsp.cancel_request` | `module.py` | Cancel an in-flight LSP request. |

Short aliases (`tool_names.py`): `LintCheck` →
`lsp.diagnostics`, `LintFile` → `lsp.check`. These are exposed
historically but the actions are still flagged `internal` — the
runtime hides them from the agent's index.

### `lsp.diagnostics` — typical response

```json
{
  "success": true,
  "data": {
    "mode": "lsp",
    "server": "pyright",
    "target": "src/auth/validate.py",
    "diagnostics": [
      {
        "severity": "error",
        "line": 42,
        "column": 12,
        "code": "reportMissingImports",
        "message": "Import 'foo' could not be resolved",
        "source": "pyright"
      }
    ],
    "total": 1,
    "errors": 1,
    "warnings": 0
  }
}
```

When called with no `path`, returns the list of active + pending
servers — useful for diagnostics panels.

## Two delivery paths to the agent

The agent does **not** call the LSP actions itself. Two automated
paths surface diagnostics:

### 1. Inline `lint` field on writes (auto)

The `filesystem` and `workspace` modules call `lsp.notify_change`
internally after every successful `write` / `edit`. The freshly
computed diagnostics are merged into the action's response under a
`lint` field. The agent reads them as part of the tool result.

### 2. `lsp_diagnose` hook (declarative)

`hooks.py` — the universal post-write hook that wraps
`lsp.notify_change`. Lets any module that writes a file (custom
writers, MCP tools, ...) get free diagnostics via one YAML hook:

```yaml
runtime:
  hooks:
    - id: auto_lint
      "on": tool_end
      # `tool_name` uses fnmatch globs, NOT regex. `|` separates
      # alternatives; `*` is a wildcard. The compiler verifies every
      # pattern against the app's known tools at deploy time.
      condition:
        type: tool_name
        match: "filesystem.write|workspace.write"
      action:
        type: lsp_diagnose
        path_field: tool.params.path
        content_field: tool.params.content
        publish: true              # push to the diagnostics preview channel
        inject_result: true        # merge lint into the tool result
        read_from_disk: false      # content comes from params
```

The `publish: true` flag pushes diagnostics to the
`diagnostics` preview channel (Socket.IO event); `inject_result`
merges them into the agent's next message so the LLM can self-
correct. See [Tool Hooks → lsp_diagnose](31-tool-hooks.md) for
the full action reference.

## Auto-detection

`module.py` `_auto_detect()`. When the YAML config is empty
or partial, the module detects which language servers are
installed and registers them automatically. Detection probes for:

| Language | Server | Detection |
|----------|--------|-----------|
| Python | `pyright-langserver --stdio` | `which pyright-langserver` |
| Python (faster lint) | `ruff-lsp` (alt) | `which ruff-lsp` or `ruff` for the CLI fallback |
| TypeScript / JavaScript | `typescript-language-server --stdio` | `which typescript-language-server` |
| Go | `gopls` | `which gopls` |
| Rust | `rust-analyzer` | `which rust-analyzer` |
| LaTeX | `texlab` | `which texlab` |

Servers are spawned lazily on the first relevant file open and
stay running for the session; closed on `cleanup_session`
(`module.py`).

## Built-in fallback validators

`packages/digitorn/modules/lsp/parsers.py`. When no LSP server is
available, the module runs **content validators** in-process — no
external tool needed:

| Validator | Files | Source |
|-----------|-------|--------|
| JSON / JSONC | `.json`, `.jsonc` | `parse_generic_json` (`parsers.py`) + native `json.loads` |
| YAML | `.yaml`, `.yml` | `pyyaml` validation |
| TOML | `.toml` | `tomllib` validation |
| Python syntax | `.py`, `.pyi` | `ast.parse` |
| Ruff (when CLI installed) | `.py` | `parse_ruff` (`parsers.py`) |

The fallbacks always run, regardless of whether an LSP server is
also configured — the runtime aggregates results from both and
deduplicates.

## CLI / compiler output parsers

`parsers.py` also ships parsers for tools that produce
text-formatted output rather than LSP messages — useful when the
runtime invokes a CLI tool (via `shell.bash`) and needs to
extract structured diagnostics:

| Parser | Source | Output format |
|--------|--------|---------------|
| `parse_ruff` | `parsers.py` | Ruff JSON output. |
| `parse_eslint` | `parsers.py` | ESLint JSON output. |
| `parse_tsc` | `parsers.py` | TypeScript compiler messages. |
| `parse_cargo` | `parsers.py` | Cargo build output. |
| `parse_govet` | `parsers.py` | `go vet` output. |
| `parse_generic_json` | `parsers.py` | Any tool that emits a JSON array of diagnostics. |
| `parse_generic_lines` | `parsers.py` | Line-based output (`file:line:col: severity: msg`). |
| `parse_fallback` | `parsers.py` | Last-resort heuristic. |
| `parse_lsp_diagnostics` | `parsers.py` | Normalises raw LSP `diagnostics` arrays into `Diagnostic` records. |

Each parser returns a list of `Diagnostic`
(`parsers.py`) records: `{severity, line, column, code,
message, source}`.

## Two protocol modes

Internally, every supported language has a `FeedbackProtocol`
with one of two modes:

- **`lsp`** — full LSP server. Real-time diagnostics, hover,
  definitions, references, completions. Server runs as a
  persistent subprocess; the runtime sends LSP RPC over stdio.
- **`compiler`** — invokes a CLI tool on demand
  (`pyright --outputjson <file>`, `ruff --output-format json`,
  ...) and parses the output. No persistent server. Slower but
  works without language-server installs.

The module picks `lsp` when the server is available, `compiler`
as the fallback. Visible in the response as `data.mode`.

## Generic LSP RPC

`lsp.request` (`module.py`) lets internal callers send any
LSP method (hover, definition, references, completions, ...)
through a connected protocol. The result is the raw LSP response
under `data.result`.

```python
# Example: hover info at a specific position
result = await lsp.request({
    "path": "src/auth/validate.py",
    "method": "textDocument/hover",
    "params": {
        "textDocument": {"uri": "file:///.../validate.py"},
        "position": {"line": 41, "character": 8},
    },
})
```

`lsp.cancel_request` (`module.py`) cancels an in-flight
request by id.

## Session lifecycle

| Stage | What happens |
|-------|--------------|
| Module load | Auto-detection probes for installed language servers. |
| First file access | The protocol for that file's extension is started lazily. |
| Per write | `notify_change` is called automatically by the writing module. |
| Session end | `cleanup_session` (`module.py`) closes every spawned server cleanly; orphaned subprocesses are killed. |

## Cross-references

- The `lsp_diagnose` hook (the canonical way to wire LSP into the
  loop):
  [Tool Hooks → Built-in actions](31-tool-hooks.md#actions-13-built-in)
- `filesystem.write` and `workspace.write` auto-trigger
  diagnostics — covered in
  [App Configuration → tools.modules](02-app-config.md#toolsmodules--module-configuration)
- Per-module deep reference (config knobs, troubleshooting):
  [modules/reference/lsp.md](../reference/modules/lsp.md)
- Workspace module (uses LSP for live preview lint):
  [Workspace & Preview](41-preview.md)
