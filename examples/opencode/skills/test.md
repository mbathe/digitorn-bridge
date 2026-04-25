# Test Runner

Detect and run the project's test suite.

1. Detect the test runner by checking for config files:
   - `package.json` → look for "test" script → `shell.run("npm test")` (or yarn/pnpm)
   - `pyproject.toml` → check for [tool.pytest] → `shell.run("pytest -v")`
   - `Cargo.toml` → `shell.run("cargo test")`
   - `go.mod` → `shell.run("go test ./...")`
   - `Makefile` → check for `test` target → `shell.run("make test")`
   - If unsure, use `shell.which("pytest")` / `shell.which("npm")` to check availability
2. If a specific file was just edited, run only related tests:
   - Python: `pytest path/to/test_file.py -v` or `pytest -k "test_name" -v`
   - JavaScript: `npm test -- --testPathPattern=filename`
   - Rust: `cargo test module_name`
   - Go: `go test ./path/to/package/...`
3. Parse the output:
   - Count passed, failed, skipped tests
   - For failures: extract the test name, expected vs actual, and the relevant source line
4. Report a summary: X passed, Y failed, Z skipped
5. If tests fail:
   - Identify the root cause from the error message
   - Suggest a fix but do NOT auto-fix unless the user asks
