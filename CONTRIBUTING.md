# Contributing to Digitorn

Thanks for your interest in contributing. Here's how to get started.

## Development Setup

```bash
git clone https://github.com/digitorn/digitorn-bridge.git
cd digitorn-bridge
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Copy the environment template and fill in any needed keys:

```bash
cp .env.example .env
```

## Running Tests

```bash
# Full suite (some tests require Ollama/Redis - they'll skip automatically)
pytest tests/

# Fast subset (no external deps)
pytest tests/module/ tests/test_security_advanced.py tests/test_app_agents.py

# Single module
pytest tests/module/test_base.py -v
```

## Project Structure

```
packages/digitorn/
  core/           # Framework core: compiler, runtime, API, CLI
    app/          # YAML schema, compiler, manager, sessions
    runtime/      # Agent loop, bootstrap, hooks, approval queue
    api/          # FastAPI endpoints
    cli/          # Typer CLI + Textual TUI
  modules/        # All pluggable modules (filesystem, shell, database, mcp, ...)
examples/         # Example YAML app definitions
tests/            # Test suite
docs/             # Documentation
```

## Making Changes

1. Fork the repo and create a feature branch from `main`.
2. Write tests for new functionality.
3. Run `pytest` and make sure nothing is broken.
4. Keep commits focused - one logical change per commit.
5. Open a pull request with a clear description of what and why.

## Code Style

- Python 3.12+, type hints everywhere.
- Use `structlog` / `logging` - no `print()` in library code.
- Pydantic models for all schemas and configs.
- `@action` decorator for module actions (single source of truth).
- Immutable dataclasses for compiled/runtime structures.

## Security

If you find a security vulnerability, please report it privately via GitHub Security Advisories instead of opening a public issue.

## Publishing

Releases are triggered by pushing a `vX.Y.Z` git tag. The
`.github/workflows/release.yml` workflow builds the wheel,
verifies it contains no private code, and publishes to PyPI.

Before tagging, run the same check locally:

```bash
poetry build
python scripts/check-wheel-contents.py dist/digitorn-*-py3-none-any.whl
```

The script rejects any wheel that contains files from private
sibling packages (`packages/auth`, `packages/digitorn-preview-sdk`,
`packages/digitorn_cli`), build artifacts, internal docs, or
secret-shaped filenames (`.pem`, `.key`, `credentials.json`,
...). If a future addition requires a new top-level entry, edit
the `FORBIDDEN_TOPLEVEL` list in the script (and the matching
`exclude` block in `pyproject.toml`).

## License

By contributing, you agree that your contributions will be licensed under the Apache-2.0 license.
