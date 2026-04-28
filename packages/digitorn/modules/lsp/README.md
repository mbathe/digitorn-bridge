# LSP Module v2

Real-time code diagnostics via persistent language servers + fallback linters.

## Dual Mode

- **Real-time**: Persistent LSP server subprocess via [sidecar](../../../docs/app-language/26-sidecar.md) (JSON-RPC stdio). Diagnostics cached from push notifications. ~5ms retrieval.
- **Fallback**: Shell-out to linters when no LSP server is installed. ~500ms per call.

## LSP Servers (Real-Time)

| Language | Server | Binary |
|----------|--------|--------|
| Python | pyright | `pyright-langserver --stdio` |
| Python | pylsp | `pylsp` |
| JS/TS | typescript-language-server | `typescript-language-server --stdio` |
| Go | gopls | `gopls` |
| Rust | rust-analyzer | `rust-analyzer` |

## Linters (Fallback)

| Language | Linter | Command |
|----------|--------|---------|
| Python | ruff | `ruff check --output-format=json` |
| Python | mypy | `mypy --output=json` |
| JS/TS | eslint | `eslint --format=json` |
| TypeScript | tsc | `tsc --noEmit` |
| Rust | cargo | `cargo check --message-format=json` |
| Go | go vet | `go vet -json` |

## Actions

- `diagnostics(path?)` - Get errors/warnings (real-time or fallback)
- `check(path)` - Quick pass/fail for a single file
- `notify_change(path)` - Notify LSP that a file changed (triggers fresh diagnostics)

## Configuration

```yaml
modules:
  lsp:
    config:
      python: "pyright-langserver --stdio"  # Real-time LSP
      typescript: "typescript-language-server --stdio"

execution:
  hooks:
    - id: lint_after_edit
      on: tool_end
      condition:
        type: tool_match
        tools: ["filesystem.edit", "filesystem.write"]
      action:
        type: module_action
        name: lsp.notify_change
        action_params:
          path: "{{tool.params.path}}"
```

See [full documentation](../../../docs/app-language/27-lsp.md).
