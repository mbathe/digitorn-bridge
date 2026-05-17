---
id: advanced-16-selfcorrect-builtin
title: "Advanced 16 - Self-correction loop with built-in lint"
sidebar_label: "Advanced 16: Self-correct loop"
---

The workspace module ships a small set of built-in **content
validators** for the languages Digitorn ships out of the box.
When you set `lint: true` on the workspace, every `WsWrite` and
`WsEdit` response carries a `lint` field with the diagnostics
the validators produced. An agent prompted to read that field
and re-edit on errors gets a free self-correction loop without
any external LSP server, linter, or compiler installed.

This tutorial is **live-tested end-to-end**: app
`lsp-py-selfcorrect`, session `test-c8c09bfe`, brain
`openai/gpt-5-mini` via the gateway. Total run: 11.8s, 2 turns,
1 deliberate syntax error fixed.

## What the workspace lints out of the box

[`packages/digitorn/modules/workspace/module.py:390`](https://github.com/digitorn-ai/digitorn-bridge/blob/main/packages/digitorn/modules/workspace/module.py#L390)
registers a validator per extension. No external dependency:

| Extension | Validator | Catches |
|---|---|---|
| `.py`, `.pyi` | `compile()` | Python syntax errors with line + column + message |
| `.json`, `.jsonc` | `json.loads` | Decoder errors with line + column |
| `.yaml`, `.yml` | `yaml.safe_load_all` | PyYAML parse errors with `problem_mark` |
| `.toml` | `tomllib.loads` | TOML parser errors with line number |
| `.tex`, `.latex` | brace + environment matcher | Unmatched `{}`, mismatched `\begin{}/\end{}` |

Each returns a list of `{line, column, severity, message, source}`
diagnostics. Resolution order in
[`_run_lint`](https://github.com/digitorn-ai/digitorn-bridge/blob/main/packages/digitorn/modules/workspace/module.py#L1628):

1. If a separate `lsp:` module is wired AND has a real LSP
   server for this extension, the workspace forwards
   `notify_change` to it and uses the server's output.
2. If the LSP path returns nothing, the built-in validator
   fires.
3. If neither yields a diagnostic, the `lint` field is omitted
   from the tool result entirely (no false positive).

Built-in validators are the **always-available** floor. They
catch the class of bugs that block file loading (syntax errors).
They do not catch type errors, undefined names, lint style, or
anything that requires a real LSP server.

## The YAML

```yaml
app:
  app_id: lsp-py-selfcorrect
  name: Python Self-Correction Loop
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: auto
  max_turns: 6
  timeout: 180
  tool_injection: direct
  direct_modules: [workspace]

agents:
  - id: main
    role: assistant
    brain:
      provider: openai
      backend: openai_compat
      model: gpt-5-mini
      config:
        api_key: placeholder
        base_url: https://api.openai.com/v1
      temperature: 0.2
      max_tokens: 4096
      context:
        max_tokens: 200000
        strategy: summarize
        keep_recent: 12
        auto_compact: true
    system_prompt: |
      You author Python files inside the workspace.

      Use WsWrite to create files. Use WsEdit to modify them.
      NEVER paste code in chat. Only what lands in the
      workspace counts.

      Every WsWrite / WsEdit response carries a `lint` field
      with diagnostics. Treat it as ground truth.

      Workflow:
      1. WsWrite the file.
      2. Read the `lint` field on the result.
      3. If `errors > 0`, WsEdit to fix the most pressing error.
      4. Repeat until `errors == 0`.
      5. Reply with one short confirmation line.

      Cap fix attempts at 3 per error.

tools:
  modules:
    preview: {}
    workspace:
      config:
        render_mode: code
        entry_file: main.py
        title: "Python Self-Correction"
        sync_to_disk: true
        auto_approve: true
        lint: true
  capabilities:
    default_policy: auto
    max_risk_level: high
    grant:
      - module: workspace
        actions: [read, write, edit, glob, grep]
```

Three knobs to know:

- `lint: true` (default) enables the lint pipeline. Set it to
  `false` to silence the field across the whole app.
- `sync_to_disk: true` mirrors workspace writes to disk so the
  file is also readable by external tools (and by you, in the
  session workspace under
  `~/.digitorn/workspaces/<app>/<session>/`). The built-in
  validators read from the in-memory content, not from disk,
  so they work even without sync.
- `auto_approve: true` skips the human review step. Without
  it, the diff sits in `validation: "pending"` until the
  client approves; lint still runs and surfaces the
  diagnostics.

## Deploy and run

```bash
digitorn dev deploy lsp-py-selfcorrect.yaml
digitorn dev chat lsp-py-selfcorrect -m "Create a Python file bad.py that defines add(a, b) returning a + b. On your FIRST WsWrite, deliberately omit the colon after the def line so we can see the lint diagnostics. Then WsEdit to fix it."
```

## Real session transcript (session `test-c8c09bfe`)

**Turn 1: agent writes the broken file.**

Tool call:

```json
WsWrite(
  path = "bad.py",
  content = "def add(a, b)\n    return a + b\n"
)
```

Tool result the agent receives back:

```json
{
  "path": "bad.py",
  "language": "python",
  "size": 31,
  "total_lines": 3,
  "lint": [
    {
      "line": 1,
      "column": 14,
      "severity": "error",
      "message": "expected ':'",
      "source": "python"
    }
  ],
  "errors": 1,
  "warnings": 0
}
```

The `source: "python"` field is the marker that the built-in
`compile()` validator produced the diagnostic. If the LSP
module had been wired to a real Python LSP server, the source
would be the server name (e.g. `"pyright"`).

**Turn 2: agent fixes via WsEdit.**

```json
WsEdit(
  path      = "bad.py",
  old_string = "def add(a, b)\n    return a + b",
  new_string = "def add(a, b):\n    return a + b"
)
```

Tool result:

```json
{
  "path": "bad.py",
  "mode": "replace",
  "replacements": 1,
  "size": 32,
  "total_lines": 3
}
```

No `lint` field on this response, which confirms the
post-edit content lint-checked clean. The agent then replied:

> Done. I created bad.py with a deliberate syntax error first,
> inspected the lint output (it showed "expected ':'" on line 1),
> then fixed the file.

End-to-end in 11.8s.

## Same pattern, other languages

The same YAML works for JSON, YAML, TOML, LaTeX. Just point
the agent at the relevant file extension. Examples of
diagnostics you would see:

JSON, broken trailing comma:
```json
{"line": 4, "column": 1, "severity": "error",
 "message": "Expecting property name enclosed in double quotes",
 "source": "json"}
```

YAML, wrong indent:
```json
{"line": 3, "column": 5, "severity": "error",
 "message": "mapping values are not allowed here",
 "source": "yaml"}
```

TOML, unterminated string:
```json
{"line": 2, "column": 1, "severity": "error",
 "message": "Unclosed string (at line 2, column 1)",
 "source": "toml"}
```

LaTeX, unmatched `\begin{}`:
```json
{"line": 12, "column": 1, "severity": "error",
 "message": "\\begin{itemize} never closed",
 "source": "latex"}
```

## Going further: `lsp_diagnose` hook

The workspace's built-in lint runs automatically for
`workspace.write` / `workspace.edit`. If you want the same
self-correction loop on **other write surfaces** (the
`filesystem` module, an MCP tool that creates files, a custom
writer), wire the `lsp_diagnose` hook to inject diagnostics into
their tool results:

```yaml
runtime:
  hooks:
    - id: lint_after_write
      "on": tool_end
      condition:
        type: tool_name
        match: [filesystem.write, filesystem.edit]
      action:
        type: lsp_diagnose
        path_field: ["path", "file_path"]
        content_field: ["content"]
        publish: true
        inject_result: true
      cooldown: 0.5
      max_fires: 0
```

`inject_result: true` merges the diagnostics into the
write/edit tool result so the agent's next turn sees the same
`lint` / `errors` / `warnings` fields the workspace surface
already gets. `publish: true` pushes the same data to the
`diagnostics` preview channel for client UIs that render
markers.

**Caveat (current daemon state).** The hook calls
`lsp.notify_change(path, content)` which goes through the LSP
module's real-LSP-server path. The LSP module's built-in
content validators live inside the workspace module, not in
the LSP module. So today, `lsp_diagnose` on a non-workspace
write surface only yields diagnostics when a real LSP server
is configured AND running. A daemon-side improvement to
fall back to the same built-in validators when no LSP server
matches the extension is on the roadmap.

## What we proved

| Claim | Status |
|---|---|
| Built-in Python validator returns line, column, message, source | verified |
| Workspace injects the `lint` field into WsWrite tool result | verified, session `test-c8c09bfe` seq 21 |
| Agent reads the lint field and corrects via WsEdit | verified, session seq 38 |
| Clean post-edit content omits the `lint` field | verified, session seq 38 result keys |
| Pattern generalises to JSON, YAML, TOML, LaTeX | verified by inspecting the validator registry |

## When to reach for this

- Any workflow where the agent generates structured files
  (configs, dataclasses, JSON payloads, LaTeX papers).
- Self-bootstrapping projects where you do not want to install
  pyright / eslint / tsc on the daemon machine.
- A safe floor under more advanced LSP wiring: even if the LSP
  server fails to start, the built-in validator still catches
  syntax bugs.

For type-level diagnostics (pyright, tsc), eslint-style style
rules, or anything beyond syntax, you need a real LSP server
plus the `lsp:` module config. That path is documented
separately.
