---
id: advanced-16-selfcorrect-builtin
title: "Advanced 16 - Self-correction loop with built-in lint"
sidebar_label: "Advanced 16: Self-correct loop"
---

The workspace module ships a small set of built-in **content
validators** for the most common file formats.
When you set `lint: true` on the workspace, every `WsWrite` and
`WsEdit` response carries a `lint` field with the diagnostics
the validators produced. An agent prompted to read that field
and re-edit on errors gets a free self-correction loop without
any external LSP server, linter, or compiler installed.

## Languages with built-in validators

| Extension | Catches |
|---|---|
| `.json`, `.jsonc` | JSON decoder errors with line + column |
| `.yaml`, `.yml` | YAML parse errors |
| `.toml` | TOML parser errors with line number |
| `.tex`, `.latex` | Unmatched `{}`, mismatched `\begin{}/\end{}` |

Each returns a list of `{line, column, severity, message, source}`
diagnostics. Built-in validators are the always-available floor.
They catch syntax errors that block file loading. They do not
catch type errors, undefined names, lint style, or anything that
requires a real LSP server.

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
        entry_file: main.ts
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
  file is also readable by external tools. The built-in
  validators read from the in-memory content, not from disk,
  so they work even without sync.
- `auto_approve: true` skips the human review step. Without
  it, the diff sits in `validation: "pending"` until the
  client approves; lint still runs and surfaces the
  diagnostics.

## Deploy and run

```bash
digitorn dev deploy lsp-py-selfcorrect.yaml
digitorn dev chat lsp-py-selfcorrect -m "Create a Python file bad.ts that defines add(a, b) returning a + b. On your FIRST WsWrite, deliberately omit the colon after the def line so we can see the lint diagnostics. Then WsEdit to fix it."
```

## Sample flow

**Turn 1: WsWrite the broken file.**

Tool call:

```json
WsWrite(
  path = "bad.ts",
  content = "def add(a, b)\n    return a + b\n"
)
```

Tool result the agent receives back:

```json
{
  "path": "bad.ts",
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

**Turn 2: WsEdit to fix.**

```json
WsEdit(
  path      = "bad.ts",
  old_string = "def add(a, b)\n    return a + b",
  new_string = "def add(a, b):\n    return a + b"
)
```

Tool result:

```json
{
  "path": "bad.ts",
  "mode": "replace",
  "replacements": 1,
  "size": 32,
  "total_lines": 3
}
```

No `lint` field on this response: the post-edit content
lint-checked clean. The agent then confirms:

> Done. I created bad.ts with a deliberate syntax error first,
> inspected the lint output, then fixed the file.

## Same pattern, other languages

The same YAML works for JSON, YAML, TOML, LaTeX. Just point
the agent at the relevant file extension. Examples of
diagnostics:

JSON, broken trailing comma:

```json
{"line": 4, "column": 1, "severity": "error",
 "message": "Expecting property name enclosed in double quotes"}
```

YAML, wrong indent:

```json
{"line": 3, "column": 5, "severity": "error",
 "message": "mapping values are not allowed here"}
```

TOML, unterminated string:

```json
{"line": 2, "column": 1, "severity": "error",
 "message": "Unclosed string (at line 2, column 1)"}
```

LaTeX, unmatched `\begin{}`:

```json
{"line": 12, "column": 1, "severity": "error",
 "message": "\\begin{itemize} never closed"}
```

## Going further: `lsp_diagnose` hook

The workspace's built-in lint runs automatically for
`workspace.write` / `workspace.edit`. If you want the same
self-correction loop on **other write surfaces** (the
`filesystem` module, an MCP tool that creates files, a custom
writer), wire the `lsp_diagnose` hook to inject diagnostics
into their tool results:

```yaml
runtime:
  hooks:
    - id: lint_after_write
      'on': tool_end
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

## When to reach for this

- Any workflow where the agent generates structured files
  (configs, dataclasses, JSON payloads, LaTeX papers).
- Self-bootstrapping projects where you do not want to install
  pyright / eslint / tsc on the daemon machine.
- A safe floor under more advanced LSP wiring: even if a
  language server fails to start, the built-in validator still
  catches syntax bugs.

For type-level diagnostics, eslint-style style rules, or
anything beyond syntax, configure a real LSP server in the
`lsp:` module.
