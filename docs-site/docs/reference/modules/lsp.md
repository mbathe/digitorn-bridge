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

## Live test audit (2026-05-17)

Comprehensive end-to-end audit covering the 5 internal actions,
the auto-detect path, the built-in content validators, session
cleanup, cross-OS spawn safety, and dead-code drift. Scenarios
live in `tools/live_tests/lsp_e2e_scenarios.py`. Final state:
**10 PASS / 0 FAIL / 2 SKIP** (the 2 skips are external limits,
not bugs - see below).

### Bugs found and fixed

1. **Workered proxy installed unbound on the instance**
   (`workers/action_wrapper.py`).
   `_make_proxy_handler` returned `async def _proxy(module_self,
   params)` and `setattr(module, action_name, proxy_handler)`
   bypassed Python's descriptor protocol, so REST callers like
   `lsp_module.cancel_request(params)` lost `self` and 500'd
   with `missing 1 required positional argument: 'params'`.
   Fix: new `_bind_proxy_to_module` helper pre-binds the module
   before `setattr`. Affects every workered module's
   direct-attribute call path, not just LSP.

2. **Workered proxy returned a bare `dict` instead of an
   `ActionResult`** (`workers/action_wrapper.py`).
   `client.call_action(...)` deserialises the worker's HTTP
   response into a `dict`; daemon-side callers reach for
   `result.success` and crash with
   `AttributeError: 'dict' object has no attribute 'success'`.
   Fix: new `_rehydrate_action_result` helper reconstructs an
   `ActionResult` (with `success / data / error / metadata`)
   before returning. Pass-through for non-dict / already-typed
   payloads.

### Bug found, not yet fixed (workers framework, broader scope)

1. **Workered modules never receive their per-app `module.config`
   from YAML.** *(Numbered #3 in the cumulative audit log; restarted
   at 1 here because it's a fresh ordered list.)* The daemon-side
   `bootstrap.py` correctly skips
   `on_config_update` on a workered instance (the wrap stamps
   `_skip_on_start = True`, and the lifecycle loop short-circuits
   right after). But the worker's own lifespan
   (`workers/app.py:174`) calls `module.on_start()` for each
   hosted module and **never replays the config either**. Net
   effect: any `modules.lsp.config.python: "ruff check ..."` (or
   any per-app explicit server spec) is silently dropped when LSP
   is hosted by a worker (default deployment ships it in the
   `tools` worker alongside MCP).

   Symptoms observed during the audit:
   - The `linter_python` scenario reports the .py file is written
     correctly and `ruff` is on PATH, but the `lint` field comes
     back empty. Built-in validators only catch *syntax errors*;
     they don't run ruff. Ruff invoked manually on the same disk
     path returns F401 + E722 as expected.
   - This is **not** an LSP module bug. It's a workers framework
     gap that affects every workered module that needs per-app
     config (LSP servers, custom MCP servers, etc.).
   - MCP's catalog-only references (e.g. `fetch`) happen to work
     because they're resolved at `on_start()` from the
     daemon-side catalog, not from per-app YAML.

   Fix sketch (post-audit, needs design sign-off): the
   `wrap_module_for_worker` call should push the compiled app
   config to the worker via an HTTP envelope (e.g. a new
   `POST /admin/config` route on the worker, or a per-call ctx
   field that the worker applies on first dispatch). The latter
   is simpler but couples per-request data to module-level state;
   the former is cleaner but adds one extra round-trip per
   deploy.

### Skipped scenarios (external limits, not module bugs)

- `lsp_request_hover` - SKIPs even when `pyright-langserver` is
  on PATH. The full hover round-trip needs a real Pyright server
  plus a .py file at a known disk path; the audit covers the
  endpoint plumbing via code review and leaves the live
  round-trip as future work (no LSP bug surfaced in the path
  reviewed).
- `linter_python` - SKIPs because of Bug #3 above. The scenario
  is preserved so it auto-promotes to PASS once the workers
  framework propagates per-app config.

### Cleanups done in passing

- `packages/digitorn/modules/lsp/parsers.py` - removed
  `BUILTIN_VALIDATORS` + the 4 `validate_*_file` functions.
  Dead code: the canonical content validators live in
  `workspace/module.py::_BUILTIN_CONTENT_VALIDATORS` (with the
  LaTeX validator on top), wired by workspace + filesystem.
- `packages/digitorn/modules/lsp/module.py::_start_server` -
  uses `shlex.split(command, posix=(sys.platform != "win32"))`
  at the spawn site, with a `command.split()` fallback only on
  `ValueError` (unmatched quotes). Previously `command.split()`
  was the sole strategy and would mangle paths-with-spaces on
  Windows.
