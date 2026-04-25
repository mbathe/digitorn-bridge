# LSP Module

Diagnostics via external linters. Auto-detects the appropriate linter based on file extension and project root markers.

## Supported Linters

| Language | Linter | Command |
|----------|--------|---------|
| Python | ruff | `ruff check --output-format=json` |
| Python | mypy | `mypy --output=json` |
| JS/TS | eslint | `eslint --format=json` |
| TypeScript | tsc | `tsc --noEmit` |
| Rust | cargo | `cargo check --message-format=json` |
| Go | go vet | `go vet -json` |

## Actions

- `diagnostics(path?)` — Get errors/warnings for a file or project
- `check(path)` — Quick pass/fail check for a single file

## Configuration

```yaml
modules:
  lsp:
    config:
      python: "ruff check --output-format=json"
      typescript: "eslint --format=json"
```
