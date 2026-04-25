# Project Detection

Detect the project stack and store it for the session.

1. Scan the workspace root for project files:
   - `package.json` → read it, extract: name, scripts (test, build, lint, dev), main dependencies
   - `pyproject.toml` → extract: project name, build system, test config, dependencies
   - `Cargo.toml` → extract: package name, edition, key dependencies
   - `go.mod` → extract: module name, go version
   - `Makefile` → list available targets (test, build, lint, clean, etc.)
   - `docker-compose.yml` or `Dockerfile` → note containerized setup
2. Check for linter/formatter config:
   - `.eslintrc*` / `biome.json` / `.prettierrc` → JS/TS linting
   - `ruff.toml` / `pyproject.toml [tool.ruff]` / `.flake8` → Python linting
   - `rustfmt.toml` / `clippy.toml` → Rust linting
3. Check for CI configuration:
   - `.github/workflows/` → read CI files for test/build/lint commands
   - `.gitlab-ci.yml` / `Jenkinsfile` / `.circleci/config.yml`
4. Check git state:
   - `shell.bash("git status")` — current branch, pending changes
   - `shell.bash("git log --oneline -5")` — recent activity
5. Look for setup docs:
   - `README.md` or `CONTRIBUTING.md` — extract setup/install/test instructions
6. Store all findings with `memory.add_fact()`:
   - Language and framework
   - Package manager
   - Test command, build command, lint command
   - Any special setup requirements
7. Report a compact summary to the user:
   - Stack: language, framework, package manager
   - Commands: test, build, lint
   - Branch: current branch, clean/dirty status
