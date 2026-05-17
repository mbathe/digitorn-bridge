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
all 3 protocol modes (LSP server JSON-RPC, compiler, linter),
the auto-detect path, the built-in content validators, real raw
LSP requests (`textDocument/hover` against pyright), real
mid-flight cancellation, per-app state isolation, session
cleanup, cross-OS spawn safety, and dead-code drift. 15
scenarios in `tools/live_tests/lsp_e2e_scenarios.py`. Final
state: **14 PASS / 0 FAIL / 1 SKIP** (the skip is
`cancel_inflight` when the race is lost -- pyright already warm
in the worker; the same scenario PASSes deterministically when
the request hits the cold-start window).

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

3. **Workered modules never received their per-app `module.config`
   from YAML.** *(Workers framework bug; affects every workered
   module that needs per-app config -- custom MCP servers, RAG
   backends, shell allow-lists -- surfaced first on LSP because
   LSP is the canonical workered module with explicit per-app
   server declarations.)* The daemon-side `bootstrap.py` correctly
   skipped
   `on_config_update` on a workered instance (the wrap stamps
   `_skip_on_start = True`, and the lifecycle loop short-circuited
   right after). But the worker's own lifespan
   (`workers/app.py:174`) called `module.on_start()` for each
   hosted module and **never replayed the config either**. Net
   effect: any `modules.lsp.config.python: "ruff check ..."` (or
   any per-app explicit server spec) was silently dropped when LSP
   was hosted by a worker (default deployment ships it in the
   `tools` worker alongside MCP).

   Impact extended far beyond LSP — every workered module that
   needs per-app config (custom MCP servers, RAG backend paths,
   shell allow-lists, web allow-domains) was affected. MCP's
   catalog-only references (e.g. `fetch`) happened to work because
   they're resolved at `on_start()` from the daemon-side catalog,
   not from per-app YAML.

   **Fix** (4 small patches, no breaking changes):
   - `workers/routes.py` -- new `POST /admin/config/{module}`
     route that takes `{config: {...}}`, auth via the shared
     bearer secret, calls `module.on_config_update(config)`
     server-side, returns `{success, error?}`.
   - `workers/client.py` -- new `WorkerClient.push_config(module,
     config)` with the same retry semantics as `call_action`.
   - `workers/registry.py` -- new `endpoints_for(module)` that
     returns ALL endpoints (multi-replica safe; the round-robin
     `route()` is only for single-call load-balancing).
   - `workers/action_wrapper.py` -- new `push_module_config()`
     helper that broadcasts the config to every endpoint hosting
     the module. Logs partial failures, never blocks the deploy.
   - `core/runtime/bootstrap.py` -- in the workered branch of the
     lifecycle loop (the `_skip_on_start` short-circuit), now
     awaits `push_module_config(module_id, cfg, registry=...)`
     with the compiled per-app config. The `WORKSPACE_PLACEHOLDER`
     template (`"{WORKSPACE}"`) is intentionally NOT pushed --
     workspace is per-session and travels in the `ctx` envelope
     on each call instead.

4. **Workspace passed workspace-relative paths to `lsp.notify_change`.**
   Linter / compiler protocols shell out to a subprocess that
   reads from disk. With a bare relative path (`test_lint.py`) and
   the LSP module's `_workspace` being a leaked `"{WORKSPACE}"`
   placeholder, the subprocess `cwd` resolved to a literal
   non-existent directory and ruff died with
   `FileNotFoundError`. Even after the placeholder fix above, the
   relative path would have made ruff fall back to its own cwd
   (the worker's process dir), which doesn't contain the file.

   **Fix** in `workspace/module.py::_run_lint`: before calling
   `lsp.notify_change`, resolve the workspace path to its absolute
   on-disk location via `_resolve_disk_dir_for(path)` +
   `_join_inside(disk_dir, path)`. LSP-server-mode protocols are
   unaffected (URIs are built downstream); linter / compiler now
   get a path that ruff / tsc / cargo can actually find. Stable
   cache keys across the workspace + lsp boundary as a bonus.

5. **`sys.platform` referenced without `import sys`** in
   `lsp/module.py::_start_server` (introduced earlier in this
   audit when replacing `command.split()` with `shlex.split`).
   Latent because the daemon's lifecycle loop skipped
   `on_config_update` on workered modules -- the `NameError`
   never had a chance to fire. Surfaced as soon as Bug #3's fix
   made `on_config_update` reach the worker for real. Added
   `import sys` at the top of the module.

6. **Workspace passed workspace-relative paths to
   `lsp.notify_change`** -- linter / compiler subprocesses read
   from disk, so a bare `test_lint.py` resolved against the
   worker process's cwd (not the session workspace) and ruff
   /tsc died with `FileNotFoundError`. Fix in
   `workspace/module.py::_run_lint`: resolve to the absolute
   on-disk path via `_resolve_disk_dir_for` + `_join_inside`
   before calling `lsp.notify_change`.

7. **Malformed `file://` URIs on Windows** -- the legacy
   `f"file://{Path(path).resolve()}"` shape produced
   `file://C:\Users\...` (two slashes, backslashes), which
   pyright / typescript-language-server accept silently on
   didOpen but then can't match against hover / goto requests
   that come back with the normalised
   `file:///C:/Users/...` shape. Replaced every URI build site
   in `lsp/{module,protocols}.py` with `Path.resolve().as_uri()`
   (RFC 8089-compliant: three slashes, forward slashes).

8. **`lsp.request` auto-opened the document but didn't wait for
   the LSP server to parse it.** First-time `textDocument/hover`
   on pyright fired right after didOpen and got back `null`
   because pyright's symbol table didn't exist yet. Fix in
   `lsp/module.py::request`: mirror the same cold-start warm-up
   `notify_change` already uses (3 s on cold-first-hit, 0.3 s
   on warm).

9. **REST `/lsp/request` and `/lsp/cancel` didn't carry the
   per-app routing identity.** REST endpoints invoke
   `lsp_module.request(...)` directly (not via
   `module.execute()`), so the daemon-side proxy's
   `_build_ctx_payload` reads an empty `_context_var` and
   tenant routing collapses to "whichever app last called
   `on_config_update`". Fix in `apps_v2/lsp.py::lsp_rpc_request`:
   stamp an `ExecutionContext(app_id=app_id, session_id=...)`
   on the contextvar before dispatching; reset in the `finally`.
   The endpoint also now resolves the request path to absolute
   (via the workspace module's `_resolve_disk_dir_for`)
   alongside activating the preview session, so didOpen reads
   the right file from the right tenant's workspace dir.

10. **`CompilerProtocol` never appended the file path to its
    argv.** Designed for `cargo check` (project-wide), but the
    docstring claimed `tsc --noEmit` and `javac` were also
    supported -- they aren't usable without the file as a
    positional. Fix in `protocols.py`: append `path` to the
    argv unless the command's basename is in
    `_PROJECT_WIDE = {"cargo", "go"}` (those genuinely
    project-wide tools choke on extra positionals).

11. **`shutil.which` not used at the spawn site** --
    `asyncio.create_subprocess_exec("tsc", ...)` on Windows
    calls `CreateProcessW` which does NOT honour `PATHEXT` for
    `.cmd` / `.bat` shims (`tsc` ships as `tsc.cmd` via npm).
    Resolved with `FileNotFoundError(2)` even though
    `shutil.which("tsc")` returned a real path. Fix in
    `protocols.py`: pre-resolve `argv[0]` with `shutil.which`
    before the spawn; applied to both `LinterProtocol` and
    `CompilerProtocol`. Defense in depth for both posix and
    Windows.

12. **Workered modules with per-app state had no app keying
    -- the canonical `app_id` bleed.** A YAML configuring
    `lsp.config.python: "ruff ..."` for app A registered the
    ruff protocol on the LSP module's flat `_protocols[".py"]`
    map; app B (no python config) writing a `.py` then got
    ruff diagnostics too. Confirmed live by the
    `state_isolation` scenario. Fix: replace
    `_protocols: dict[ext, protocol]` with
    `_app_protocols: dict[app_id, dict[ext, protocol]]` (same
    for `_protocol_instances` and `_pending_specs`); keep
    legacy `_protocols`/`_pending_specs` as read-only
    `@property` aggregations so introspection and the existing
    `on_stop` clean-up still work. `on_config_update` accepts
    an `app_id` kwarg threaded through from the worker's
    `/admin/config/{module}` route via
    `WorkerClient.push_config` and bootstrap. `_get_protocol`
    looks up by the active `ExecutionContext.app_id` (with
    `self._app_id` as fallback). Clean isolation: an app that
    didn't configure a server for the extension gets `None`
    -- no cross-tenant inheritance.

13. **REST endpoints bypass `module.execute()` and lose the
    `_context_var` envelope** -- a class of latent bugs that
    surfaced through Bug #12. Workspace's `_run_lint` now
    stamps its own `ExecutionContext` (with
    `app_id=self._app_id_override`) around its
    `self._lsp.notify_change(...)` call so the daemon-side
    proxy ships the right tenant. `_build_ctx_payload` in
    `workers/action_wrapper.py` also reads
    `module_self._app_id_override` (set by
    `_inject_app_id_overrides`) as a fallback when
    `_context_var` is empty, so any other REST entry point
    that touches a per-app module's `_app_id_override` slot
    gets the same protection by default.

### Skipped scenarios (timing limits, not module bugs)

- `cancel_inflight` - SKIPs when the LSP server (pyright) is
  already warm in the worker and the hover response races ahead
  of the cancel POST. The cancel endpoint correctly reports
  request-not-found in that case; the scenario flips to PASS
  whenever the request hits pyright's cold-start window. Both
  paths exercise the same in-flight tracking dict, so coverage
  is intact -- the only variable is which arm fires first on a
  given run.

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
