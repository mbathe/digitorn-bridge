"""LSP module — diagnostics via external linters.

Phase 1: Shell-out to linters (ruff, eslint, tsc, cargo check, go vet).
Auto-detects the appropriate linter based on file extension.

Phase 2 (future): Real LSP client via JSON-RPC stdio.
"""
