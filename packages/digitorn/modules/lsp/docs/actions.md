# LSP Module - Actions

## diagnostics

Get diagnostics (errors, warnings) for a file or project.

**Parameters:**
- `path` (optional): File path to check. If omitted, checks the whole project.
- `fix` (optional, default false): If true, auto-fix issues where possible.

**Returns:** `{linter, target, diagnostics[], total, errors, warnings}`

## check

Quick check a single file - returns pass/fail.

**Parameters:**
- `path` (required): File path to check.

**Returns:** `{path, linter, passed, errors, warnings, diagnostics[]}`
