---
id: module-concept-lsp
title: "lsp module — overview"
type: module-concept
module: lsp
isolation: shared
keywords: [lsp, lsp-module]
version: 3.0.0
---

# `lsp` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `3.0.0`
- **Actions**: 0 visible, 5 internal

## Description (from class docstring)

LSP module v3 — Universal real-time feedback for any language.

Fully dynamic: every YAML entry = a feedback channel. Supports 3 modes:
  - ``lsp``: JSON-RPC persistent (pyright, gopls, texlab, rust-analyzer)
  - ``compiler``: Re-run after each edit (cargo check, tsc --noEmit)
  - ``linter``: Shell-out on-demand (ruff, eslint, stylelint)

Config examples::

    # Minimal — auto-detect from root markers
    lsp: {}

    # Simple — auto-detect protocol from command name
    lsp:
      config:
        python: "pyright-langserver --stdio"
        rust: "cargo check --message-format=json"

    # Full control
    lsp:
      config:
        servers:
          python:
            command: "pyright-langserver --stdio"
            protocol: lsp
            extensions: [".py", ".pyi"]
          latex:
            command: "texlab"
            protocol: lsp
            extensions: [".tex", ".bib"]

> Class-level summary: Universal real-time feedback — any language, any tool.

    v3: Fully dynamic configuration. 3 protocol modes.
    Auto-detects project language and available tools.

## Configuration

Set under `modules.lsp.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon. |
| `servers` | dict |  | `{}` | Named language server configs (command, protocol, extensions, ...). |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `diagnostics` | `LintCheck` | ✓ | low | Get diagnostics (errors, warnings) for a file or project. Uses real-time LSP if available, falls back to compiler or ... |
| `check` | `LintFile` | ✓ | low | Quick pass/fail check for a single file. Internal — called by hooks/middleware, not by the LLM agent. |
| `notify_change` | `LspNotifyChange` | ✓ | low | Notify that a file was changed — triggers fresh diagnostics. Internal — called automatically by the workspace/filesys... |
| `request` | `LspRequest` | ✓ | low | Forward a raw LSP request (hover / goto / references / completion / rename / …) to the language server backing a give... |
| `cancel_request` | `LspCancelRequest` | ✓ | low | Cancel an in-flight LSP request by request_id. Internal — called by the REST /lsp/cancel endpoint. |

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/lsp-*.md`.
